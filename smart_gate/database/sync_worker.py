"""
Sinkronisasi Database Lokal PostgreSQL <-> Supabase Cloud (Thread Background)

Setiap putaran (config.SYNC_INTERVAL detik, atau segera saat ada log/perubahan baru):
  1. Kirim log kendaraan yang belum terkirim          (lokal -> Supabase)
  2. Kirim perubahan kendaraan lokal                  (lokal -> Supabase: tambah/ubah/hapus/auto-bind RFID)
  3. Ambil daftar kendaraan terbaru                   (Supabase -> lokal: pendaftaran via Netlify /register)
  4. Ambil akun petugas (tiap 60 detik)               (Supabase -> lokal: login dashboard lokal saat offline)

Perintah buka gerbang dari dashboard Netlify (tabel gate_commands) dipantau di thread
terpisah setiap 1 detik. Perintah yang lebih tua dari GATE_COMMAND_MAX_AGE detik
(misal dikirim saat Mini PC offline) ditandai EXPIRED agar palang tidak terbuka tiba-tiba.

Saat internet terputus, semua data tetap tersimpan di lokal dan dikirim setelah online kembali.
"""

import time
import threading
from datetime import datetime, timezone
import requests
import config
from smart_gate.database.local_db import LocalDBError, LOG_FIELDS, VEHICLE_FIELDS, _parse_ts

USERS_PULL_INTERVAL = 60


class SyncWorker:
    def __init__(self, db, commands, start_threads=True):
        self.db = db
        self.commands = commands  # queue.Queue perintah buka gerbang untuk main.py
        self.url = getattr(config, 'SUPABASE_URL', '').rstrip('/')
        self.key = getattr(config, 'SUPABASE_KEY', '')
        self.enabled = bool(self.url and self.key and 'YOUR_PROJECT_ID' not in self.url and 'YOUR_SUPABASE' not in self.key)
        self.interval = getattr(config, 'SYNC_INTERVAL', 10)
        self.command_max_age = getattr(config, 'GATE_COMMAND_MAX_AGE', 60)

        self.online = False
        self.last_sync = None
        self.last_error = None
        self.has_log_uuid = None            # Kolom vehicle_logs.client_uuid tersedia di Supabase?
        self.has_vehicle_updated_at = None  # Kolom registered_vehicles.updated_at tersedia di Supabase?
        self._last_users_pull = 0.0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

        if not self.enabled:
            print("[INFO] Supabase belum dikonfigurasi: sistem berjalan 100% lokal tanpa sinkronisasi cloud.")
            return
        if not start_threads:
            return  # Dipakai setup_local_db.py: sinkronisasi sekali tanpa mengambil perintah buka gerbang

        threading.Thread(target=self._sync_loop, daemon=True).start()
        threading.Thread(target=self._command_loop, daemon=True).start()

    # ── Utilitas ───────────────────────────────────
    def _req(self, method, path, timeout=8, headers=None, **kwargs):
        """
        Request ke Supabase REST. Gangguan sementara (5xx, 401/403 key salah, 429 rate limit)
        dilempar sebagai exception agar data dicoba ulang nanti, bukan dianggap ditolak.
        """
        merged = dict(self.headers)
        merged.update(headers or {})
        res = requests.request(method, f"{self.url}/rest/v1/{path}", headers=merged, timeout=timeout, **kwargs)
        if res.status_code >= 500 or res.status_code in (401, 403, 429):
            raise requests.HTTPError(f"HTTP {res.status_code}", response=res)
        return res

    def _set_online(self, online: bool, error: str = None):
        if online != self.online:
            if online:
                print("[SYNC] Internet tersedia: sinkronisasi Supabase aktif.")
            else:
                print(f"[SYNC] Supabase tidak terjangkau ({error}). Data tetap disimpan di database lokal.")
        self.online = online
        self.last_error = error

    def request_sync(self):
        """Minta sinkronisasi segera (misal setelah ada log baru)."""
        self._wake.set()

    # ── Putaran Sinkronisasi ───────────────────────
    def _sync_loop(self):
        while not self._stop.is_set():
            self.sync_once()
            self._wake.wait(self.interval)
            self._wake.clear()

    def sync_once(self) -> bool:
        with self._lock:
            try:
                self._detect_capabilities()
                self._push_logs()
                self._push_vehicles()
                self._pull_vehicles()
                if time.time() - self._last_users_pull >= USERS_PULL_INTERVAL:
                    self._pull_users()
                    self._last_users_pull = time.time()
                self.last_sync = datetime.now(timezone.utc)
                self._set_online(True)
                return True
            except requests.RequestException as e:
                self._set_online(False, e.__class__.__name__)
            except LocalDBError as e:
                self.last_error = f"database lokal: {e}"
            return False

    def _detect_capabilities(self):
        changed = False
        # Kolom yang belum ada dijawab PostgREST dengan HTTP 400
        if self.has_log_uuid is None:
            self.has_log_uuid = self._req('GET', 'vehicle_logs?select=client_uuid&limit=1', timeout=5).status_code == 200
            changed = True
        if self.has_vehicle_updated_at is None:
            self.has_vehicle_updated_at = self._req('GET', 'registered_vehicles?select=updated_at&limit=1', timeout=5).status_code == 200
            changed = True
        if changed and not (self.has_log_uuid and self.has_vehicle_updated_at):
            print("[WARN] Kolom sinkronisasi belum ada di Supabase. Jalankan supabase/migration_sync.sql di "
                  "SQL Editor Supabase agar log tidak berisiko terkirim dobel & konflik data terdeteksi akurat.")

    def _push_logs(self):
        while True:
            rows = self.db.pending_logs(100)
            if not rows:
                return
            if self._post_logs(rows):
                print(f"  [SYNC] {len(rows)} log kendaraan terkirim ke Supabase.")
                continue
            # Batch ditolak karena data (400/409): kirim satu per satu agar satu baris bermasalah tidak menahan antrean
            for row in rows:
                if not self._post_logs([row]):
                    print(f"  [SYNC WARN] Log #{row['id']} ({row['plate']}) ditolak Supabase, disimpan lokal saja.")
                    self.db.mark_logs_synced([row['id']])

    def _post_logs(self, rows) -> bool:
        """Return True jika terkirim, False jika ditolak karena data (gangguan koneksi melempar exception)."""
        payload = []
        for r in rows:
            item = {k: r[k] for k in LOG_FIELDS}
            item["confidence"] = float(item["confidence"])
            item["created_at"] = r["created_at"].isoformat()  # Waktu kejadian asli, bukan waktu terkirim
            if self.has_log_uuid:
                item["client_uuid"] = str(r["client_uuid"])
            payload.append(item)

        if self.has_log_uuid:
            path, prefer = 'vehicle_logs?on_conflict=client_uuid', 'resolution=ignore-duplicates,return=minimal'
        else:
            path, prefer = 'vehicle_logs', 'return=minimal'
        res = self._req('POST', path, json=payload, headers={"Prefer": prefer})
        if res.status_code not in (200, 201, 204):
            return False
        self.db.mark_logs_synced([r['id'] for r in rows])
        return True

    def _push_vehicles(self):
        for v in self.db.dirty_vehicles():
            body = {k: v[k] for k in VEHICLE_FIELDS}

            if v["deleted"]:
                if v["remote_id"] is not None:
                    res = self._req('DELETE', f"registered_vehicles?id=eq.{v['remote_id']}")
                    if res.status_code not in (200, 204):
                        self._vehicle_rejected(v, res)
                        continue
                self.db.purge_vehicle(v["id"])
                print(f"  [SYNC] Kendaraan {v['plate']} dihapus dari Supabase.")
                continue

            row = None
            if v["remote_id"] is not None:
                res = self._req('PATCH', f"registered_vehicles?id=eq.{v['remote_id']}", json=body,
                                headers={"Prefer": "return=representation"})
                if res.status_code != 200:
                    self._vehicle_rejected(v, res)
                    continue
                rows = res.json()
                row = rows[0] if rows else None  # Kosong = baris sudah dihapus di Supabase, kirim ulang sebagai baru
            if row is None:
                res = self._req('POST', 'registered_vehicles?on_conflict=plate', json=body,
                                headers={"Prefer": "resolution=merge-duplicates,return=representation"})
                if res.status_code not in (200, 201):
                    self._vehicle_rejected(v, res)
                    continue
                row = res.json()[0]
            self.db.mark_vehicle_synced(v["id"], row["id"])
            print(f"  [SYNC] Kendaraan {v['plate']} tersinkron ke Supabase.")

    def _vehicle_rejected(self, v, res):
        # Konflik (misal RFID sudah dipakai di Supabase): versi Supabase dipakai pada pull berikutnya
        print(f"  [SYNC WARN] Perubahan kendaraan {v['plate']} ditolak Supabase "
              f"({res.status_code}): {res.text[:120]}")
        self.db.mark_vehicle_synced(v["id"])

    def _pull_vehicles(self):
        res = self._req('GET', 'registered_vehicles?select=*&order=id.asc')
        if res.status_code != 200:
            return
        stats = self.db.apply_remote_vehicles(res.json())
        changes = {k: n for k, n in stats.items() if k != "skipped" and n}
        if changes:
            print(f"  [SYNC] Kendaraan dari Supabase: {changes}")

    def _pull_users(self):
        res = self._req('GET', 'users?select=id,name,email,password,role')
        if res.status_code == 200:
            self.db.apply_remote_users(res.json())

    def refresh_vehicles_now(self, timeout=3.0) -> bool:
        """Ambil segera daftar kendaraan Supabase (saat plat tidak ditemukan di lokal, misal baru daftar)."""
        if not self.enabled or not self.online:
            return False
        if not self._lock.acquire(timeout=timeout):
            return False
        try:
            res = self._req('GET', 'registered_vehicles?select=*&order=id.asc', timeout=timeout)
            if res.status_code == 200:
                self.db.apply_remote_vehicles(res.json())
                return True
        except (requests.RequestException, LocalDBError):
            pass
        finally:
            self._lock.release()
        return False

    # ── Perintah Buka Gerbang dari Netlify ─────────
    def _command_loop(self):
        while not self._stop.is_set():
            try:
                res = self._req('GET', 'gate_commands?status=eq.PENDING&order=created_at.asc&limit=5', timeout=4)
                if res.status_code == 200:
                    for cmd in res.json():
                        self._claim_command(cmd)
                self._stop.wait(1.0)
            except requests.RequestException:
                self._stop.wait(5.0)

    def _claim_command(self, cmd):
        created = _parse_ts(cmd.get("created_at"))
        age = (datetime.now(timezone.utc) - created).total_seconds() if created else 0
        expired = age > self.command_max_age
        status = "EXPIRED" if expired else "EXECUTED"

        # Filter status=eq.PENDING mencegah satu perintah dieksekusi dua kali
        res = self._req('PATCH', f"gate_commands?id=eq.{cmd['id']}&status=eq.PENDING",
                        json={"status": status, "executed_at": datetime.now(timezone.utc).isoformat()},
                        headers={"Prefer": "return=representation"}, timeout=4)
        if res.status_code != 200 or not res.json():
            return
        if expired:
            print(f"  [REMOTE] Perintah buka gerbang dari {cmd.get('operator_name')} diabaikan "
                  f"(sudah {int(age)} detik, batas {self.command_max_age} detik).")
            return
        self.commands.put({**cmd, "source": "NETLIFY"})

    # ── Status ─────────────────────────────────────
    def status(self) -> dict:
        try:
            pending = self.db.count_pending_logs()
        except LocalDBError:
            pending = None
        return {
            "cloud_enabled": self.enabled,
            "online": self.online,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
            "pending_logs": pending,
            "supabase_migration_ok": bool(self.has_log_uuid and self.has_vehicle_updated_at),
            "last_error": self.last_error,
        }

    def stop(self):
        self._stop.set()
        self._wake.set()
