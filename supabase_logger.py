"""
Modul untuk mengintegrasikan sistem ALPR dengan Supabase (PostgreSQL Cloud).
Koneksi langsung dari Raspberry Pi / PC via REST API HTTPS — 100% tanpa MQTT.

Alur Kerja Otomatis:
1. Kamera menangkap gambar & Plate Recognizer membaca plat.
2. lookup_vehicle() mencocokkan plat dengan database registered_vehicles di Supabase.
3. log_vehicle() mengirimkan data lengkap (Full Data: Plat, Pemilik, Kategori, NIM/NIP, Status, Confidence, Direction) ke vehicle_logs.
"""

import time
import requests
from datetime import datetime
import config


def normalize_plate(plate: str) -> str:
    """Normalisasi plat nomor: hilangkan spasi/simbol dan jadikan huruf kapital."""
    if not plate:
        return ""
    return "".join(c for c in plate if c.isalnum()).upper()


class SupabaseLogger:
    def __init__(self):
        self.url = getattr(config, 'SUPABASE_URL', '').rstrip('/')
        self.key = getattr(config, 'SUPABASE_KEY', '')
        self.enabled = bool(
            self.url 
            and self.key 
            and not 'YOUR_PROJECT_ID' in self.url
            and not 'YOUR_SUPABASE' in self.key
        )

        self.registered_cache = []
        self.last_cache_refresh = 0
        self.cache_ttl = 60  # Cache kendaraan terdaftar selama 60 detik

        # Fallback offline data jika Supabase belum dikonfigurasi
        self.fallback_vehicles = [
            {"id": 1, "plate": "B 3782 NHF", "owner": "Elkan Baggot", "owner_name": "Elkan Baggot", "category": "STUDENT", "nim_nip": "3332200001", "rfid_uid": "0001234567"},
            {"id": 2, "plate": "B 1234 VZN", "owner": "Shabrina Zahra", "owner_name": "Shabrina Zahra", "category": "STUDENT", "nim_nip": "3332200002", "rfid_uid": "0002345678"},
            {"id": 3, "plate": "B 3501 NZN", "owner": "Rocky Alfanz", "owner_name": "Rocky Alfanz", "category": "LECTURER", "nim_nip": "198501012010121001", "rfid_uid": "0003456789"},
            {"id": 4, "plate": "A 9816 XY", "owner": "Bundo", "owner_name": "Bundo", "category": "ACADEMIC COMMUNITY", "nim_nip": "199002022015042001", "rfid_uid": "0004567890"},
            {"id": 5, "plate": "A 2145 JZ", "owner": "Sohari", "owner_name": "Sohari", "category": "ACADEMIC COMMUNITY", "nim_nip": "199103032016051002", "rfid_uid": "0005678901"}
        ]

        if not self.enabled:
            print("[INFO] Supabase belum aktif (masih URL default). Menggunakan offline fallback matching.")
            return

        self.headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }

        # Tes koneksi awal dan muat cache kendaraan terdaftar
        try:
            r = requests.get(f"{self.url}/rest/v1/registered_vehicles?select=*&limit=5", headers=self.headers, timeout=5)
            if r.status_code in (200, 206):
                print("[OK] Supabase Cloud Database terhubung!")
                self.refresh_registered_cache()
            else:
                print(f"[WARN] Respon Supabase status {r.status_code}: {r.text[:80]}")
        except Exception as e:
            print(f"[WARN] Belum dapat terhubung ke Supabase: {e}")

    @staticmethod
    def _clean_rfid(val) -> str:
        """Bersihkan nilai rfid_uid; kembalikan string kosong jika NULL / belum di-bind."""
        if val is None:
            return ""
        s = str(val).strip().upper()
        if s in ("", "NONE", "NULL", "PENDING", "-", "WAITING"):
            return ""
        return s

    def refresh_registered_cache(self, force: bool = False):
        """
        Sinkronkan daftar registered_vehicles dari Supabase ke memory lokal.
        Membuat pencocokan plat di Raspberry Pi berjalan instan (~0ms).
        """
        if not self.enabled:
            return

        now = time.time()
        if not force and (now - self.last_cache_refresh < self.cache_ttl):
            return

        try:
            url = f"{self.url}/rest/v1/registered_vehicles?select=*"
            res = requests.get(url, headers=self.headers, timeout=5)
            if res.status_code == 200:
                self.registered_cache = res.json()
                self.last_cache_refresh = now
        except Exception as e:
            print(f"  [WARN] Gagal memperbarui cache kendaraan: {e}")

    def bind_rfid_to_vehicle(self, row_id, plate_text: str, rfid_uid: str) -> bool:
        """
        Otomatis mengikat (Auto-Bind) UID kartu RFID baru ke kendaraan yang sudah
        mendaftar mandiri via Web Portal (status rfid_uid sebelumnya NULL/kosong).
        """
        clean_uid = self._clean_rfid(rfid_uid)
        if not clean_uid:
            return False

        # 1. Update ke Supabase jika terhubung
        if self.enabled:
            try:
                if row_id is not None:
                    patch_url = f"{self.url}/rest/v1/registered_vehicles?id=eq.{row_id}"
                else:
                    patch_url = f"{self.url}/rest/v1/registered_vehicles?plate=eq.{requests.utils.quote(plate_text)}"
                res = requests.patch(patch_url, headers=self.headers, json={"rfid_uid": clean_uid}, timeout=5)
                if res.status_code in (200, 204):
                    print(f"  [AUTO-BIND SUPABASE OK] Kartu RFID '{clean_uid}' resmi terikat ke plat '{plate_text}'!")
                    # Update cache lokal secara langsung
                    target_norm = normalize_plate(plate_text)
                    for item in self.registered_cache:
                        if normalize_plate(item.get("plate", "")) == target_norm:
                            item["rfid_uid"] = clean_uid
                    return True
                else:
                    print(f"  [AUTO-BIND WARN] Gagal update rfid_uid di Supabase ({res.status_code}): {res.text[:100]}")
            except Exception as e:
                print(f"  [AUTO-BIND ERROR] Koneksi gagal saat auto-bind RFID: {e}")

        # 2. Update di fallback lokal jika mode simulasi/offline
        target_norm = normalize_plate(plate_text)
        for item in self.fallback_vehicles:
            if normalize_plate(item.get("plate", "")) == target_norm:
                item["rfid_uid"] = clean_uid
                return True
        return False

    def lookup_vehicle(self, plate_text: str) -> dict:
        """
        Mencocokkan plat yang terdeteksi OCR dengan data di registered_vehicles.
        Menggunakan normalisasi karakter (mengabaikan spasi dan perbedaan huruf kapital/kecil).
        """
        target_norm = normalize_plate(plate_text)
        if not target_norm:
            return {
                "matched": False,
                "plate": plate_text,
                "owner_name": "Tamu / Belum Terdaftar",
                "category": "ACADEMIC COMMUNITY",
                "nim_nip": "-",
                "rfid_uid": "",
                "needs_rfid_bind": False,
                "status": "TIDAK TERDAFTAR"
            }

        # 1. Mode Supabase Aktif
        if self.enabled:
            self.refresh_registered_cache()

            for attempt in range(2):
                for item in self.registered_cache:
                    db_norm = normalize_plate(item.get("plate", ""))
                    if db_norm == target_norm:
                        db_rfid = self._clean_rfid(item.get("rfid_uid"))
                        return {
                            "matched": True,
                            "id": item.get("id"),
                            "plate": item.get("plate", plate_text),
                            "owner_name": item.get("owner") or item.get("owner_name") or "Civitas Untirta",
                            "category": item.get("category", "STUDENT"),
                            "nim_nip": item.get("nim_nip", "-") or "-",
                            "rfid_uid": db_rfid,
                            "needs_rfid_bind": (not bool(db_rfid)),
                            "status": "TERDAFTAR"
                        }
                if attempt == 0:
                    # Jika tidak ada di cache, refresh paksa (siapa tahu baru saja daftar via QR HP)
                    self.refresh_registered_cache(force=True)

        # 2. Mode Demo / Fallback Offline
        for item in self.fallback_vehicles:
            fb_norm = normalize_plate(item.get("plate", ""))
            if fb_norm == target_norm:
                db_rfid = self._clean_rfid(item.get("rfid_uid"))
                return {
                    "matched": True,
                    "id": item.get("id"),
                    "plate": item.get("plate", plate_text),
                    "owner_name": item.get("owner") or item.get("owner_name") or "Civitas Untirta",
                    "category": item.get("category", "STUDENT"),
                    "nim_nip": item.get("nim_nip", "-") or "-",
                    "rfid_uid": db_rfid,
                    "needs_rfid_bind": (not bool(db_rfid)),
                    "status": "TERDAFTAR"
                }

        # Plat tidak ditemukan dalam registered_vehicles
        return {
            "matched": False,
            "plate": plate_text.strip().upper(),
            "owner_name": "Tamu / Belum Terdaftar",
            "category": "ACADEMIC COMMUNITY",
            "nim_nip": "-",
            "rfid_uid": "",
            "needs_rfid_bind": False,
            "status": "TIDAK TERDAFTAR"
        }

    def lookup_rfid(self, rfid_uid: str) -> dict:
        """
        Mencocokkan UID kartu RFID dengan tabel registered_vehicles di Supabase.
        """
        clean_uid = self._clean_rfid(rfid_uid)
        if not clean_uid:
            return {
                "matched": False,
                "plate": "NO_RFID",
                "owner_name": "Kartu Tidak Valid",
                "category": "ACADEMIC COMMUNITY",
                "nim_nip": "-",
                "status": "TIDAK TERDAFTAR",
                "method": "RFID"
            }

        # 1. Mode Supabase Aktif
        if self.enabled:
            self.refresh_registered_cache()
            for item in self.registered_cache:
                if self._clean_rfid(item.get("rfid_uid")) == clean_uid:
                    return {
                        "matched": True,
                        "plate": item.get("plate", "RFID_CARD"),
                        "owner_name": item.get("owner") or item.get("owner_name") or "Civitas Untirta",
                        "category": item.get("category", "STUDENT"),
                        "nim_nip": item.get("nim_nip", "-") or "-",
                        "status": "TERDAFTAR",
                        "method": "RFID"
                    }

        # 2. Mode Demo / Fallback Offline
        for item in self.fallback_vehicles:
            if self._clean_rfid(item.get("rfid_uid")) == clean_uid:
                return {
                    "matched": True,
                    "plate": item.get("plate", "RFID_CARD"),
                    "owner_name": item.get("owner") or item.get("owner_name") or "Civitas Untirta",
                    "category": item.get("category", "STUDENT"),
                    "nim_nip": item.get("nim_nip", "-") or "-",
                    "status": "TERDAFTAR",
                    "method": "RFID"
                }

        return {
            "matched": False,
            "plate": f"RFID-{clean_uid[:8]}",
            "owner_name": "Kartu Belum Terdaftar",
            "category": "ACADEMIC COMMUNITY",
            "nim_nip": "-",
            "status": "TIDAK TERDAFTAR",
            "method": "RFID"
        }

    def _is_rfid_used_by_other(self, rfid_uid: str, current_plate_norm: str) -> bool:
        """Cek apakah UID kartu RFID sudah terikat pada kendaraan/plat nomor lain."""
        clean_uid = self._clean_rfid(rfid_uid)
        if not clean_uid:
            return False
        source_list = self.registered_cache if (self.enabled and self.registered_cache) else self.fallback_vehicles
        for item in source_list:
            item_rfid = self._clean_rfid(item.get("rfid_uid"))
            item_plate = normalize_plate(item.get("plate", ""))
            if item_rfid and item_rfid == clean_uid and item_plate != current_plate_norm:
                return True
        return False

    def verify_dual_auth(self, plate_text: str, rfid_uid: str) -> dict:
        """
        Verifikasi GANDA + Auto-Bind Tap Pertama:
        1. Jika Plat terdaftar dan RFID cocok -> verified = True (reason = 'MATCH')
        2. Jika Plat terdaftar via Portal Mandiri (rfid_uid masih NULL/kosong) dan kartu RFID
           yang di-tap belum dipakai plat lain -> Otomatis Auto-Bind RFID ke plat tersebut!
           verified = True (reason = 'AUTO_BOUND', auto_bound = True)
        3. Jika Plat terdaftar tetapi RFID salah / milik orang lain -> verified = False ('RFID_MISMATCH')
        4. Jika Plat belum terdaftar di sistem -> verified = False ('UNREGISTERED')
        """
        target_plate = normalize_plate(plate_text)
        target_rfid = self._clean_rfid(rfid_uid)

        if not target_plate or not target_rfid:
            return {
                "verified": False,
                "matched": False,
                "auto_bound": False,
                "plate": plate_text or "UNKNOWN",
                "owner_name": "Tamu / Belum Terdaftar",
                "category": "ACADEMIC COMMUNITY",
                "nim_nip": "-",
                "rfid_uid": target_rfid or "-",
                "status": "TIDAK TERDAFTAR",
                "reason": "PARAM_EMPTY"
            }

        matched_row = None

        # 1. Cek di Supabase jika aktif
        if self.enabled:
            self.refresh_registered_cache()
            for item in self.registered_cache:
                if normalize_plate(item.get("plate", "")) == target_plate:
                    matched_row = item
                    break

            # Refresh paksa jika belum ketemu (mendukung pendaftaran baru < 1 menit yang lalu)
            if not matched_row:
                self.refresh_registered_cache(force=True)
                for item in self.registered_cache:
                    if normalize_plate(item.get("plate", "")) == target_plate:
                        matched_row = item
                        break

        # 2. Cek di Fallback Data jika belum ketemu dan offline
        if not matched_row:
            for item in self.fallback_vehicles:
                if normalize_plate(item.get("plate", "")) == target_plate:
                    matched_row = item
                    break

        if matched_row:
            db_rfid = self._clean_rfid(matched_row.get("rfid_uid"))
            owner = matched_row.get("owner") or matched_row.get("owner_name") or "Civitas Untirta"
            plate_canonical = matched_row.get("plate", plate_text)
            category = matched_row.get("category", "STUDENT")
            nim_nip = matched_row.get("nim_nip", "-") or "-"

            # KASUS A: Kendaraan baru daftar mandiri via Web (rfid_uid masih kosong) -> AUTO-BIND!
            if not db_rfid:
                if self._is_rfid_used_by_other(target_rfid, target_plate):
                    return {
                        "verified": False,
                        "matched": False,
                        "auto_bound": False,
                        "plate": plate_canonical,
                        "owner_name": owner,
                        "category": category,
                        "nim_nip": nim_nip,
                        "rfid_uid": target_rfid,
                        "status": "TIDAK COCOK",
                        "reason": "RFID_MISMATCH"
                    }
                # Lakukan Auto-Bind RFID ke baris kendaraan ini
                self.bind_rfid_to_vehicle(matched_row.get("id"), plate_canonical, target_rfid)
                return {
                    "verified": True,
                    "matched": True,
                    "auto_bound": True,
                    "plate": plate_canonical,
                    "owner_name": owner,
                    "category": category,
                    "nim_nip": nim_nip,
                    "rfid_uid": target_rfid,
                    "status": "TERDAFTAR",
                    "reason": "AUTO_BOUND"
                }

            # KASUS B: Kendaraan sudah memiliki RFID terdaftar -> Cocokkan!
            if db_rfid == target_rfid:
                return {
                    "verified": True,
                    "matched": True,
                    "auto_bound": False,
                    "plate": plate_canonical,
                    "owner_name": owner,
                    "category": category,
                    "nim_nip": nim_nip,
                    "rfid_uid": db_rfid,
                    "status": "TERDAFTAR",
                    "reason": "MATCH"
                }
            else:
                return {
                    "verified": False,
                    "matched": False,
                    "auto_bound": False,
                    "plate": plate_canonical,
                    "owner_name": owner,
                    "category": category,
                    "nim_nip": nim_nip,
                    "rfid_uid": target_rfid,
                    "status": "TIDAK COCOK",
                    "reason": "RFID_MISMATCH"
                }

        # KASUS C: Plat belum terdaftar sama sekali di sistem
        return {
            "verified": False,
            "matched": False,
            "auto_bound": False,
            "plate": plate_text.strip().upper(),
            "owner_name": "Tamu / Belum Terdaftar",
            "category": "ACADEMIC COMMUNITY",
            "nim_nip": "-",
            "rfid_uid": target_rfid,
            "status": "TIDAK TERDAFTAR",
            "reason": "UNREGISTERED"
        }

    def log_vehicle(self, plate_text: str, confidence: float = 100.0, region: str = "id", direction: str = None, method: str = "ALPR", custom_info: dict = None) -> dict:
        """
        Mencocokkan plat/RFID ke registered_vehicles dan mengirimkan data LENGKAP ke vehicle_logs.
        Jika kendaraan berstatus TIDAK TERDAFTAR dan sempat men-tap kartu RFID baru di gerbang,
        UID RFID disimpan pada field nim_nip dengan format 'RFID:<UID>' agar fitur Quick-Register
        di Web Dashboard Satpam dapat langsung mengisi otomatis Plat + RFID UID tersebut!
        """
        if direction is None:
            direction = getattr(config, 'GATE_DIRECTION', 'OUT')
        conf_val = round(confidence * 100 if confidence <= 1.0 else confidence, 2)
        
        # 1. COCOKKAN KE REGISTERED VEHICLES (Jika tidak diberikan custom_info)
        info = custom_info or self.lookup_vehicle(plate_text)

        nim_val = info.get("nim_nip", "-") or "-"
        tapped_rfid = self._clean_rfid(info.get("rfid_uid"))
        if info.get("status") == "TIDAK TERDAFTAR" and nim_val == "-" and tapped_rfid and tapped_rfid != "MOCK_RFID_TAP":
            nim_val = f"RFID:{tapped_rfid}"

        payload = {
            "plate": info["plate"],
            "name": info["owner_name"],
            "category": info["category"],
            "nim_nip": nim_val,
            "status": info["status"],
            "method": method,
            "direction": direction,
            "confidence": conf_val
        }

        # Tampilkan hasil pencocokan di console RPi
        if info.get("matched"):
            bind_tag = " [AUTO-BIND BARU]" if info.get("auto_bound") else ""
            print(f"  [MATCHED OK]{bind_tag} Terdaftar: {info['plate']} | {info['owner_name']} ({info['category']} - NIM/NIP: {info['nim_nip']}) [{method}]")
        else:
            print(f"  [UNREGISTERED] Tidak Terdaftar: {info['plate']} | Status: {info['status']} [{method}]")

        # 2. KIRIM FULL DATA KE SUPABASE
        if not self.enabled:
            print(f"  [SIMULASI LOCAL] Plat {info['plate']} dicatat secara lokal (Supabase belum diisi di config.py).")
            return payload

        try:
            url = f"{self.url}/rest/v1/vehicle_logs"
            res = requests.post(url, headers=self.headers, json=payload, timeout=6)

            if res.status_code in (200, 201):
                print(f"  [SUPABASE OK] Data terkirim penuh ke vehicle_logs! (Status: {info['status']})")
                return res.json()
            else:
                print(f"  [SUPABASE ERROR] Status {res.status_code}: {res.text}")
        except Exception as e:
            print(f"  [SUPABASE ERROR] Gagal mengirim data ke Supabase: {e}")

        return payload

    def get_pending_gate_command(self) -> dict:
        """
        Cek apakah ada perintah buka gerbang manual dari Web Dashboard via Supabase.
        Mengambil 1 perintah berstatus 'PENDING', lalu mengubah statusnya menjadi 'EXECUTED'.
        """
        if not self.enabled:
            return None

        try:
            url = f"{self.url}/rest/v1/gate_commands?status=eq.PENDING&order=created_at.asc&limit=1"
            res = requests.get(url, headers=self.headers, timeout=3)
            if res.status_code == 200:
                rows = res.json()
                if rows and len(rows) > 0:
                    cmd = rows[0]
                    cmd_id = cmd.get("id")
                    # Update status menjadi EXECUTED
                    patch_url = f"{self.url}/rest/v1/gate_commands?id=eq.{cmd_id}"
                    patch_payload = {
                        "status": "EXECUTED",
                        "executed_at": datetime.now().isoformat()
                    }
                    requests.patch(patch_url, headers=self.headers, json=patch_payload, timeout=3)
                    return cmd
        except Exception:
            pass
        return None

