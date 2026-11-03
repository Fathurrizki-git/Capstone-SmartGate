"""
Database Gerbang Keluar (Lokal-First + Sinkronisasi Supabase)

Alur data:
1. Verifikasi plat + RFID membaca database lokal PostgreSQL (instan, tetap jalan saat offline).
2. Log kendaraan disimpan ke database lokal, lalu sync_worker.py mengirimnya ke Supabase.
3. Pendaftaran kendaraan dari Netlify (/register) ditarik dari Supabase setiap SYNC_INTERVAL detik,
   dan langsung ditarik saat plat tidak ditemukan (mendukung pendaftaran beberapa detik yang lalu).
4. Perintah buka gerbang (dashboard lokal & Netlify) masuk ke satu antrean untuk main.py.
"""

import time
import queue
import requests
import config
from smart_gate.database.local_db import LocalDB, LocalDBError, normalize_plate, clean_rfid
from smart_gate.database.sync_worker import SyncWorker

VALID_CATEGORIES = ("STUDENT", "LECTURER", "ACADEMIC COMMUNITY")


def _unregistered(plate_text: str, rfid_uid: str = "", reason: str = "UNREGISTERED") -> dict:
    return {
        "verified": False,
        "matched": False,
        "auto_bound": False,
        "plate": (plate_text or "UNKNOWN").strip().upper(),
        "owner_name": "Tamu / Belum Terdaftar",
        "category": "ACADEMIC COMMUNITY",
        "nim_nip": "-",
        "rfid_uid": rfid_uid or "",
        "needs_rfid_bind": False,
        "status": "TIDAK TERDAFTAR",
        "reason": reason,
    }


def _vehicle_info(row: dict) -> dict:
    rfid = clean_rfid(row.get("rfid_uid"))
    return {
        "matched": True,
        "id": row["id"],
        "plate": row["plate"],
        "owner_name": row.get("owner") or "Civitas Untirta",
        "category": row.get("category") or "STUDENT",
        "nim_nip": row.get("nim_nip") or "-",
        "rfid_uid": rfid,
        "needs_rfid_bind": not rfid,
        "status": "TERDAFTAR",
    }


class GateDatabase:
    def __init__(self):
        self.local = LocalDB()
        self.commands = queue.Queue()
        self.sync = SyncWorker(self.local, self.commands)
        self._last_db_warning = 0.0

    def _warn_db(self, err):
        if time.time() - self._last_db_warning > 30:
            print(f"[ERROR] Database lokal PostgreSQL tidak dapat diakses: {err}. "
                  "Pastikan service PostgreSQL berjalan. Gerbang hanya dapat dibuka manual.")
            self._last_db_warning = time.time()

    def _find_plate(self, plate_text: str):
        try:
            return self.local.find_vehicle_by_plate(plate_text)
        except LocalDBError as e:
            self._warn_db(e)
            raise

    # ── Verifikasi Gerbang (dipakai main.py) ───────
    def lookup_vehicle(self, plate_text: str) -> dict:
        """Cocokkan plat hasil OCR dengan kendaraan terdaftar (mengabaikan spasi & huruf besar/kecil)."""
        if not normalize_plate(plate_text):
            return _unregistered(plate_text)
        try:
            row = self._find_plate(plate_text)
            if row is None and self.sync.refresh_vehicles_now():
                row = self._find_plate(plate_text)  # Siapa tahu baru saja daftar via Netlify / QR HP
        except LocalDBError:
            return _unregistered(plate_text, reason="DB_OFFLINE")
        return _vehicle_info(row) if row else _unregistered(plate_text)

    def verify_dual_auth(self, plate_text: str, rfid_uid: str) -> dict:
        """
        Verifikasi GANDA + Auto-Bind Tap Pertama:
        1. Plat terdaftar dan RFID cocok -> verified (reason = 'MATCH')
        2. Plat terdaftar via Portal Mandiri (rfid_uid masih kosong) dan kartu belum dipakai plat lain
           -> RFID otomatis diikat ke plat tersebut (reason = 'AUTO_BOUND')
        3. Plat terdaftar tetapi RFID salah / milik orang lain -> ditolak ('RFID_MISMATCH')
        4. Plat belum terdaftar -> ditolak ('UNREGISTERED'); database lokal mati -> ('DB_OFFLINE')
        """
        target_rfid = clean_rfid(rfid_uid)
        if not normalize_plate(plate_text) or not target_rfid:
            return _unregistered(plate_text, target_rfid or "-", reason="PARAM_EMPTY")

        info = self.lookup_vehicle(plate_text)
        if not info["matched"]:
            info.update(rfid_uid=target_rfid)
            return info

        result = {**info, "verified": False, "auto_bound": False, "rfid_uid": target_rfid}

        # KASUS A: Kendaraan baru daftar mandiri (rfid_uid masih kosong) -> AUTO-BIND
        if not info["rfid_uid"]:
            try:
                owner_of_card = self.local.find_vehicle_by_rfid(target_rfid)
                if owner_of_card and owner_of_card["id"] != info["id"]:
                    return {**result, "matched": False, "status": "TIDAK COCOK", "reason": "RFID_MISMATCH"}
                self.local.bind_rfid(info["id"], target_rfid)
            except LocalDBError as e:
                self._warn_db(e)
                return _unregistered(plate_text, target_rfid, reason="DB_OFFLINE")
            self.sync.request_sync()
            print(f"  [AUTO-BIND OK] Kartu RFID '{target_rfid}' resmi terikat ke plat '{info['plate']}'!")
            return {**result, "verified": True, "auto_bound": True, "reason": "AUTO_BOUND"}

        # KASUS B: Kendaraan sudah memiliki RFID -> harus sama persis
        if info["rfid_uid"] == target_rfid:
            return {**result, "verified": True, "reason": "MATCH"}
        return {**result, "matched": False, "status": "TIDAK COCOK", "reason": "RFID_MISMATCH"}

    def log_vehicle(self, plate_text: str, confidence: float = 100.0, region: str = "id", direction: str = None,
                    method: str = "ALPR", custom_info: dict = None) -> dict:
        """
        Simpan log kendaraan ke database lokal (lalu dikirim ke Supabase oleh sync worker).
        Jika kendaraan TIDAK TERDAFTAR tetapi sempat men-tap kartu RFID, UID disimpan pada nim_nip
        dengan format 'RFID:<UID>' agar Quick-Register di dashboard dapat mengisi Plat + RFID otomatis.
        """
        if direction is None:
            direction = getattr(config, 'GATE_DIRECTION', 'OUT')
        conf_val = round(confidence * 100 if confidence <= 1.0 else confidence, 2)
        info = custom_info or self.lookup_vehicle(plate_text)

        nim_val = info.get("nim_nip", "-") or "-"
        tapped_rfid = clean_rfid(info.get("rfid_uid"))
        if info.get("status") == "TIDAK TERDAFTAR" and nim_val == "-" and tapped_rfid:
            nim_val = f"RFID:{tapped_rfid}"

        payload = {
            "plate": info["plate"],
            "name": info["owner_name"],
            "category": info["category"],
            "nim_nip": nim_val,
            "status": info["status"],
            "method": method,
            "direction": direction,
            "confidence": conf_val,
            "region": region,
        }

        if info.get("matched"):
            bind_tag = " [AUTO-BIND BARU]" if info.get("auto_bound") else ""
            print(f"  [MATCHED OK]{bind_tag} Terdaftar: {info['plate']} | {info['owner_name']} "
                  f"({info['category']} - NIM/NIP: {info['nim_nip']}) [{method}]")
        else:
            print(f"  [UNREGISTERED] Tidak Terdaftar: {info['plate']} | Status: {info['status']} [{method}]")

        try:
            self.local.insert_log(payload)
            sync_note = "dikirim ke Supabase di latar belakang" if self.sync.online else "antre, dikirim saat internet tersedia"
            print(f"  [DB LOKAL OK] Log tersimpan ({sync_note}).")
            self.sync.request_sync()
        except LocalDBError as e:
            self._warn_db(e)
            self._post_log_direct(payload)
        return payload

    def _post_log_direct(self, payload: dict):
        """Cadangan saat PostgreSQL lokal mati: kirim log langsung ke Supabase agar tidak hilang."""
        if not self.sync.enabled:
            return
        try:
            res = requests.post(f"{self.sync.url}/rest/v1/vehicle_logs", headers=self.sync.headers,
                                json=payload, timeout=6)
            if res.status_code in (200, 201):
                print("  [SUPABASE OK] Log dikirim langsung ke Supabase (database lokal tidak tersedia).")
                return
        except requests.RequestException:
            pass
        print("  [ERROR] Log tidak tersimpan: database lokal & Supabase sama-sama tidak tersedia.")

    def get_pending_gate_command(self):
        """Ambil 1 perintah buka gerbang (dashboard lokal / Netlify) tanpa menunggu."""
        try:
            return self.commands.get_nowait()
        except queue.Empty:
            return None

    # ── API Dashboard Lokal (dipakai stream_server.py) ──
    def login(self, email: str, password: str):
        user = self.local.authenticate(email, password)
        if not user:
            return None
        token = self.local.create_session(user["email"])
        return {"token": token, "user": {"name": user["name"], "email": user["email"], "role": user["role"]}}

    def session_user(self, token: str):
        return self.local.get_session_user(token)

    def logout(self, token: str):
        self.local.delete_session(token)

    def list_logs(self, limit=150):
        return self.local.list_logs(min(max(int(limit), 1), 1000))

    def list_vehicles(self):
        return self.local.list_vehicles()

    def save_vehicle(self, data: dict, vehicle_id=None) -> dict:
        plate = (data.get("plate") or "").strip().upper()
        owner = (data.get("owner") or "").strip()
        category = (data.get("category") or "STUDENT").strip().upper()
        rfid = clean_rfid(data.get("rfid_uid"))
        if len(normalize_plate(plate)) < 3:
            raise ValueError("Plat nomor kendaraan wajib diisi dengan benar.")
        if len(owner) < 2:
            raise ValueError("Nama pemilik wajib diisi.")
        if category not in VALID_CATEGORIES:
            raise ValueError("Kategori harus STUDENT, LECTURER, atau ACADEMIC COMMUNITY.")

        if rfid:
            card_owner = self.local.find_vehicle_by_rfid(rfid)
            if card_owner and card_owner["plate_norm"] != normalize_plate(plate) \
                    and str(card_owner["id"]) != str(vehicle_id or ""):
                raise ValueError(f"Kartu RFID {rfid} sudah terdaftar pada plat {card_owner['plate']} ({card_owner['owner']}).")

        row = self.local.save_vehicle(
            {"plate": plate, "owner": owner, "category": category,
             "nim_nip": (data.get("nim_nip") or "").strip(), "rfid_uid": rfid},
            vehicle_id=vehicle_id)
        self.sync.request_sync()
        return row

    def delete_vehicle(self, vehicle_id) -> bool:
        ok = self.local.mark_vehicle_deleted(vehicle_id)
        self.sync.request_sync()
        return ok

    def open_gate(self, operator_name: str, reason: str = "", plate: str = ""):
        """Perintah buka gerbang dari dashboard lokal: langsung masuk antrean (tanpa lewat internet)."""
        self.commands.put({"operator_name": operator_name, "reason": reason, "plate": plate, "source": "LOCAL"})

    def status(self) -> dict:
        return {**self.sync.status(), "local_db": self.local.available}

    def close(self):
        self.sync.stop()
        self.local.close()
