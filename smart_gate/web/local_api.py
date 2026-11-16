"""
API Dashboard Lokal (http://<IP-Mini-PC>:5050/api/...)

Dipakai index.html saat dashboard dibuka dari server Mini PC, sehingga login, log,
kendaraan, dan buka gerbang tetap berfungsi tanpa internet (data dari PostgreSQL lokal).

Endpoint (semua kecuali /api/login wajib header  Authorization: Bearer <token>):
  POST /api/login            {email, password}            -> {token, user}
  POST /api/logout
  GET  /api/me                                            -> {user}
  GET  /api/logs?limit=150                                -> {logs}
  GET  /api/logs/trend?since=<ISO>&until=<ISO>            -> {times} (created_at kendaraan OUT, untuk Trendline)
  GET  /api/vehicles                                      -> {vehicles}
  POST /api/vehicles         {id?, plate, owner, category, nim_nip, rfid_uid} -> {vehicle}
  POST /api/vehicles/delete  {id}
  POST /api/gate/open        {reason, plate}              -> palang dibuka langsung (tanpa internet)
  GET  /api/sync                                          -> status database lokal & sinkronisasi Supabase
"""

import json
from decimal import Decimal
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from smart_gate.database.local_db import LocalDBError

try:
    from psycopg import IntegrityError
except ImportError:
    IntegrityError = ()

MAX_BODY = 64 * 1024
VEHICLE_KEYS = ("id", "plate", "owner", "category", "nim_nip", "rfid_uid", "created_at", "updated_at")
LOG_KEYS = ("id", "created_at", "plate", "name", "category", "nim_nip", "status", "method",
            "direction", "confidence", "region", "client_uuid")


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def send_json(handler, status: int, obj):
    data = json.dumps(obj, default=_json_default).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _read_json(handler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length > MAX_BODY:
        raise ValueError("Data yang dikirim terlalu besar.")
    raw = handler.rfile.read(length) if length else b""
    body = json.loads(raw or b"{}")
    if not isinstance(body, dict):
        raise ValueError("Format data tidak valid.")
    return body


def _parse_iso(value: str) -> datetime:
    """ISO 8601 dari JS (toISOString, akhiran 'Z') → datetime ber-zona waktu."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("zona waktu wajib")
    return dt


def _token(handler) -> str:
    auth = handler.headers.get("Authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else ""


def _vehicle_out(row):
    return {**{k: row[k] for k in VEHICLE_KEYS}, "synced": not row["dirty"]}


def _log_out(row):
    return {**{k: row[k] for k in LOG_KEYS}, "synced": row["synced"]}


def handle(handler, method: str, path: str, db):
    if db is None:
        return send_json(handler, 503, {"error": "API lokal tidak aktif."})
    query = parse_qs(urlparse(handler.path).query)

    try:
        if method == "POST" and path == "/api/login":
            body = _read_json(handler)
            result = db.login(body.get("email", ""), body.get("password", ""))
            if result:
                return send_json(handler, 200, result)
            if db.local.count_users() == 0:
                msg = "Belum ada akun di database lokal. Sambungkan internet sekali agar akun tersinkron dari Supabase."
            else:
                msg = "Email atau password salah."
            return send_json(handler, 401, {"error": msg})

        token = _token(handler)
        user = db.session_user(token)
        if not user:
            return send_json(handler, 401, {"error": "Sesi login berakhir, silakan login kembali."})

        if method == "GET" and path == "/api/me":
            return send_json(handler, 200, {"user": user})
        if method == "POST" and path == "/api/logout":
            db.logout(token)
            return send_json(handler, 200, {"ok": True})
        if method == "GET" and path == "/api/logs":
            limit = int(query.get("limit", ["150"])[0])
            return send_json(handler, 200, {"logs": [_log_out(r) for r in db.list_logs(limit)]})
        if method == "GET" and path == "/api/logs/trend":
            try:
                since = _parse_iso(query.get("since", [""])[0])
                until = _parse_iso(query.get("until", [""])[0])
            except ValueError:
                return send_json(handler, 400, {"error": "Parameter since/until harus format ISO 8601."})
            if until <= since or (until - since).days > 62:
                return send_json(handler, 400, {"error": "Rentang waktu tidak valid (maksimal 62 hari)."})
            return send_json(handler, 200, {"times": db.list_out_times(since, until)})
        if method == "GET" and path == "/api/vehicles":
            return send_json(handler, 200, {"vehicles": [_vehicle_out(r) for r in db.list_vehicles()]})
        if method == "POST" and path == "/api/vehicles":
            body = _read_json(handler)
            row = db.save_vehicle(body, vehicle_id=body.get("id") or None)
            return send_json(handler, 200, {"vehicle": _vehicle_out(row)})
        if method == "POST" and path == "/api/vehicles/delete":
            body = _read_json(handler)
            if not db.delete_vehicle(body.get("id")):
                return send_json(handler, 404, {"error": "Kendaraan tidak ditemukan."})
            return send_json(handler, 200, {"ok": True})
        if method == "POST" and path == "/api/gate/open":
            body = _read_json(handler)
            db.open_gate(user["name"], body.get("reason", ""), body.get("plate", ""))
            return send_json(handler, 200, {"ok": True})
        if method == "GET" and path == "/api/sync":
            return send_json(handler, 200, db.status())

        return send_json(handler, 404, {"error": "Endpoint tidak ditemukan."})

    except ValueError as e:
        return send_json(handler, 400, {"error": str(e)})
    except LocalDBError as e:
        return send_json(handler, 503, {"error": f"Database lokal tidak dapat diakses: {e}"})
    except IntegrityError as e:
        return send_json(handler, 409, {"error": "Data bentrok dengan data yang sudah ada (plat / RFID sudah terdaftar).",
                                        "detail": str(e).splitlines()[0]})
