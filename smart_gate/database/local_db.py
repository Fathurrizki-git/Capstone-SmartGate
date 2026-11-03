"""
Database Lokal PostgreSQL (Mini PC Gerbang Keluar)

Seluruh operasi gerbang (verifikasi plat + RFID, log, login dashboard lokal) membaca
dan menulis ke database lokal ini terlebih dahulu, sehingga gerbang tetap berfungsi
walaupun internet terputus. Sinkronisasi ke Supabase dikerjakan sync_worker.py.

Tabel dibuat otomatis dari local_schema.sql saat pertama kali terhubung.
"""

import os
import uuid
import hmac
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
import config

try:
    from psycopg import OperationalError
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool, PoolTimeout
    HAS_PSYCOPG = True
    CONNECTION_ERRORS = (OperationalError, PoolTimeout)
except ImportError:
    HAS_PSYCOPG = False
    CONNECTION_ERRORS = ()

SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_schema.sql")
VEHICLE_FIELDS = ("plate", "owner", "category", "nim_nip", "rfid_uid")
LOG_FIELDS = ("plate", "name", "category", "nim_nip", "status", "method", "direction", "confidence", "region")
PBKDF2_ITERATIONS = 200_000


class LocalDBError(Exception):
    """Database lokal tidak dapat diakses (service PostgreSQL mati / belum dikonfigurasi)."""


def normalize_plate(plate: str) -> str:
    """Normalisasi plat nomor: hilangkan spasi/simbol dan jadikan huruf kapital."""
    if not plate:
        return ""
    return "".join(c for c in plate if c.isalnum()).upper()


def clean_rfid(val) -> str:
    """Bersihkan nilai rfid_uid; kembalikan string kosong jika NULL / belum di-bind."""
    if val is None:
        return ""
    s = str(val).strip().upper()
    if s in ("", "NONE", "NULL", "PENDING", "-", "WAITING"):
        return ""
    return s


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(check, digest)
    except (ValueError, AttributeError):
        return False


class LocalDB:
    """Akses database lokal PostgreSQL dengan connection pool (aman dipakai banyak thread)."""

    def __init__(self, conninfo: str = None):
        self.pool = None
        self._schema_ready = False

        if not HAS_PSYCOPG:
            print("[ERROR] Driver PostgreSQL belum terpasang (pip install -r requirements.txt).")
            return

        conninfo = conninfo or getattr(config, 'LOCAL_DB_URL', '')
        # Pool tetap mencoba menyambung di background bila PostgreSQL belum menyala
        self.pool = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=6,
            timeout=3,
            open=True,
            check=ConnectionPool.check_connection,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
        try:
            self._ensure_schema()
            print("[OK] Database lokal PostgreSQL terhubung.")
        except LocalDBError as e:
            print(f"[WARN] Database lokal belum dapat diakses: {e}")

    # ── Koneksi ────────────────────────────────────
    def _ensure_schema(self):
        if self._schema_ready:
            return
        with open(SCHEMA_FILE, encoding="utf-8") as f:
            schema_sql = f.read()
        try:
            with self.pool.connection() as conn:
                conn.execute(schema_sql)
        except Exception as e:
            raise LocalDBError(_first_line(e))
        self._schema_ready = True

    def _query(self, sql, params=(), one=False):
        if self.pool is None:
            raise LocalDBError("driver PostgreSQL tidak tersedia")
        self._ensure_schema()
        try:
            with self.pool.connection() as conn:
                # Tanpa parameter: karakter '%' di SQL (misal LIKE 'a%') tidak dianggap placeholder
                cur = conn.execute(sql, params or None)
                if cur.description is None:
                    return None
                return cur.fetchone() if one else cur.fetchall()
        except CONNECTION_ERRORS as e:
            raise LocalDBError(_first_line(e))

    @property
    def available(self) -> bool:
        try:
            self._query("SELECT 1", one=True)
            return True
        except Exception:
            return False

    def close(self):
        if self.pool is not None:
            self.pool.close()

    # ── Kendaraan Terdaftar ────────────────────────
    def find_vehicle_by_plate(self, plate: str):
        return self._query(
            "SELECT * FROM registered_vehicles WHERE plate_norm = %s AND NOT deleted",
            (normalize_plate(plate),), one=True)

    def find_vehicle_by_rfid(self, rfid_uid: str):
        return self._query(
            "SELECT * FROM registered_vehicles WHERE rfid_uid = %s AND NOT deleted",
            (clean_rfid(rfid_uid),), one=True)

    def list_vehicles(self, include_deleted=False):
        where = "" if include_deleted else "WHERE NOT deleted"
        return self._query(f"SELECT * FROM registered_vehicles {where} ORDER BY id DESC")

    def save_vehicle(self, data: dict, vehicle_id=None) -> dict:
        """
        Tambah / ubah kendaraan dari gerbang atau dashboard lokal (ditandai dirty untuk dikirim ke Supabase).
        Plat yang pernah dihapus lokal (belum terkirim) dihidupkan kembali agar tidak bentrok UNIQUE.
        """
        values = {k: data.get(k) for k in VEHICLE_FIELDS}
        values["rfid_uid"] = clean_rfid(values["rfid_uid"]) or None
        values["nim_nip"] = values["nim_nip"] or None

        if vehicle_id is None:
            existing = self._query(
                "SELECT id FROM registered_vehicles WHERE plate_norm = %s",
                (normalize_plate(values["plate"]),), one=True)
            vehicle_id = existing["id"] if existing else None

        if vehicle_id is not None:
            row = self._query(
                """UPDATE registered_vehicles
                   SET plate = %(plate)s, owner = %(owner)s, category = %(category)s, nim_nip = %(nim_nip)s,
                       rfid_uid = %(rfid_uid)s, updated_at = NOW(), dirty = TRUE, deleted = FALSE
                   WHERE id = %(id)s RETURNING *""",
                {**values, "id": vehicle_id}, one=True)
            if row:
                return row
        return self._query(
            """INSERT INTO registered_vehicles (plate, owner, category, nim_nip, rfid_uid, dirty)
               VALUES (%(plate)s, %(owner)s, %(category)s, %(nim_nip)s, %(rfid_uid)s, TRUE) RETURNING *""",
            values, one=True)

    def bind_rfid(self, vehicle_id, rfid_uid: str):
        return self._query(
            """UPDATE registered_vehicles SET rfid_uid = %s, updated_at = NOW(), dirty = TRUE
               WHERE id = %s RETURNING *""",
            (clean_rfid(rfid_uid), vehicle_id), one=True)

    def mark_vehicle_deleted(self, vehicle_id) -> bool:
        # rfid_uid dilepas agar kartu dapat langsung dipakai kendaraan lain
        row = self._query(
            """UPDATE registered_vehicles SET deleted = TRUE, dirty = TRUE, rfid_uid = NULL, updated_at = NOW()
               WHERE id = %s RETURNING id""",
            (vehicle_id,), one=True)
        return row is not None

    # ── Sinkronisasi Kendaraan (dipakai sync_worker.py) ──
    def dirty_vehicles(self):
        return self._query("SELECT * FROM registered_vehicles WHERE dirty ORDER BY id")

    def mark_vehicle_synced(self, vehicle_id, remote_id=None):
        self._query(
            "UPDATE registered_vehicles SET dirty = FALSE, remote_id = COALESCE(%s, remote_id) WHERE id = %s",
            (remote_id, vehicle_id))

    def purge_vehicle(self, vehicle_id):
        self._query("DELETE FROM registered_vehicles WHERE id = %s", (vehicle_id,))

    def apply_remote_vehicles(self, remote_rows: list) -> dict:
        """
        Samakan data lokal dengan daftar kendaraan Supabase.
        - Baris lokal yang belum terkirim (dirty) tidak ditimpa, kecuali versi Supabase lebih baru.
        - Baris yang sudah dihapus di Supabase ikut dihapus di lokal.
        """
        stats = {"inserted": 0, "updated": 0, "deleted": 0, "skipped": 0}
        local_rows = self.list_vehicles(include_deleted=True)
        by_remote = {r["remote_id"]: r for r in local_rows if r["remote_id"] is not None}
        by_plate = {r["plate_norm"]: r for r in local_rows}
        remote_ids = set()

        for rv in remote_rows:
            remote_ids.add(rv["id"])
            local = by_remote.get(rv["id"]) or by_plate.get(normalize_plate(rv.get("plate", "")))
            values = {k: rv.get(k) for k in VEHICLE_FIELDS}
            values["rfid_uid"] = clean_rfid(values["rfid_uid"]) or None
            remote_updated = _parse_ts(rv.get("updated_at")) or _parse_ts(rv.get("created_at"))

            try:
                if local is None:
                    self._query(
                        """INSERT INTO registered_vehicles
                           (remote_id, plate, owner, category, nim_nip, rfid_uid, created_at, updated_at)
                           VALUES (%(remote_id)s, %(plate)s, %(owner)s, %(category)s, %(nim_nip)s, %(rfid_uid)s,
                                   COALESCE(%(created_at)s, NOW()), COALESCE(%(updated_at)s, NOW()))""",
                        {**values, "remote_id": rv["id"], "created_at": _parse_ts(rv.get("created_at")),
                         "updated_at": remote_updated})
                    stats["inserted"] += 1
                    continue

                remote_is_newer = rv.get("updated_at") is not None and remote_updated and remote_updated > local["updated_at"]
                if local["dirty"] and not remote_is_newer:
                    stats["skipped"] += 1  # Perubahan lokal menang, akan dikirim pada putaran berikutnya
                    continue

                changed = any((local.get(k) or None) != (values[k] or None) for k in VEHICLE_FIELDS) \
                    or local["remote_id"] != rv["id"] or local["deleted"] or local["dirty"]
                if changed:
                    self._query(
                        """UPDATE registered_vehicles
                           SET remote_id = %(remote_id)s, plate = %(plate)s, owner = %(owner)s, category = %(category)s,
                               nim_nip = %(nim_nip)s, rfid_uid = %(rfid_uid)s, updated_at = COALESCE(%(updated_at)s, NOW()),
                               dirty = FALSE, deleted = FALSE
                           WHERE id = %(id)s""",
                        {**values, "remote_id": rv["id"], "updated_at": remote_updated, "id": local["id"]})
                    stats["updated"] += 1
            except Exception as e:
                if isinstance(e, LocalDBError):
                    raise
                stats["skipped"] += 1
                print(f"  [SYNC WARN] Kendaraan '{rv.get('plate')}' dari Supabase tidak dapat disimpan lokal: "
                      f"{_first_line(e)}")

        for local in local_rows:
            if local["remote_id"] is not None and local["remote_id"] not in remote_ids and not local["dirty"]:
                self.purge_vehicle(local["id"])
                stats["deleted"] += 1
        return stats

    # ── Log Kendaraan ──────────────────────────────
    def insert_log(self, payload: dict) -> dict:
        values = {k: payload.get(k) for k in LOG_FIELDS}
        values["region"] = values["region"] or "id"
        values["client_uuid"] = str(uuid.uuid4())
        return self._query(
            """INSERT INTO vehicle_logs (client_uuid, plate, name, category, nim_nip, status, method, direction, confidence, region)
               VALUES (%(client_uuid)s, %(plate)s, %(name)s, %(category)s, %(nim_nip)s, %(status)s, %(method)s,
                       %(direction)s, %(confidence)s, %(region)s) RETURNING *""",
            values, one=True)

    def list_logs(self, limit=150):
        return self._query("SELECT * FROM vehicle_logs ORDER BY created_at DESC, id DESC LIMIT %s", (int(limit),))

    def pending_logs(self, limit=100):
        return self._query("SELECT * FROM vehicle_logs WHERE NOT synced ORDER BY id LIMIT %s", (int(limit),))

    def count_pending_logs(self) -> int:
        return self._query("SELECT COUNT(*) AS n FROM vehicle_logs WHERE NOT synced", one=True)["n"]

    def mark_logs_synced(self, log_ids: list):
        if log_ids:
            self._query("UPDATE vehicle_logs SET synced = TRUE WHERE id = ANY(%s)", (list(log_ids),))

    # ── Akun Petugas & Sesi Login ──────────────────
    def apply_remote_users(self, remote_users: list) -> int:
        """Salin akun dari Supabase; password di-hash ulang hanya jika berubah."""
        local = {u["email"].lower(): u for u in self._query("SELECT * FROM users")}
        seen = set()
        for ru in remote_users:
            email = (ru.get("email") or "").strip().lower()
            if not email or not ru.get("password"):
                continue
            seen.add(email)
            current = local.get(email)
            same_password = current is not None and verify_password(ru["password"], current["password_hash"])
            if same_password and current["name"] == ru.get("name") and current["role"] == ru.get("role"):
                continue
            pw_hash = current["password_hash"] if same_password else hash_password(ru["password"])
            self._query(
                """INSERT INTO users (remote_id, name, email, password_hash, role, updated_at)
                   VALUES (%s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (email) DO UPDATE SET remote_id = EXCLUDED.remote_id, name = EXCLUDED.name,
                       password_hash = EXCLUDED.password_hash, role = EXCLUDED.role, updated_at = NOW()""",
                (ru.get("id"), ru.get("name") or email, email, pw_hash, ru.get("role") or "SATPAM"))
        for email in set(local) - seen:
            self._query("DELETE FROM users WHERE email = %s", (email,))
        return len(seen)

    def count_users(self) -> int:
        return self._query("SELECT COUNT(*) AS n FROM users", one=True)["n"]

    def authenticate(self, email: str, password: str):
        user = self._query("SELECT * FROM users WHERE email = %s", ((email or "").strip().lower(),), one=True)
        if user and verify_password(password or "", user["password_hash"]):
            return user
        return None

    def create_session(self, email: str, hours=12) -> str:
        token = secrets.token_hex(32)
        self._query("DELETE FROM sessions WHERE expires_at < NOW()")
        self._query("INSERT INTO sessions (token, user_email, expires_at) VALUES (%s, %s, %s)",
                    (token, email, datetime.now(timezone.utc) + timedelta(hours=hours)))
        return token

    def get_session_user(self, token: str):
        if not token:
            return None
        return self._query(
            """SELECT u.name, u.email, u.role FROM sessions s JOIN users u ON u.email = s.user_email
               WHERE s.token = %s AND s.expires_at > NOW()""",
            (token,), one=True)

    def delete_session(self, token: str):
        self._query("DELETE FROM sessions WHERE token = %s", (token,))


def _first_line(exc) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else exc.__class__.__name__


def _parse_ts(value):
    """Ubah timestamp ISO dari Supabase menjadi datetime (timezone-aware)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
