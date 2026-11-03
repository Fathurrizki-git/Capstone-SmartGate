"""
Setup Database Lokal PostgreSQL (jalankan SEKALI di Mini PC setelah PostgreSQL terpasang)

    python scripts/setup_local_db.py

1. Membuat user & database sesuai config.LOCAL_DB_URL
   (membutuhkan password superuser 'postgres' yang diisi saat instalasi PostgreSQL)
2. Membuat tabel dari local_schema.sql
3. Menarik data awal dari Supabase (akun petugas & kendaraan) agar login dashboard lokal
   langsung bisa dipakai, termasuk saat offline setelahnya.

Aman dijalankan ulang (user/database yang sudah ada tidak dihapus, password user disamakan dengan config).
"""

import os
import sys
import queue
import getpass
import argparse
from urllib.parse import urlparse, unquote
import psycopg
from psycopg import sql
# Folder utama proyek ke path agar config & smart_gate dapat di-import
# (bisa dijalankan: python scripts/<nama>.py  atau  python -m scripts.<nama>)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from smart_gate.database.local_db import LocalDB
from smart_gate.database.sync_worker import SyncWorker


def create_role_and_database(admin_user: str, admin_password: str):
    url = urlparse(config.LOCAL_DB_URL)
    app_user = unquote(url.username or "")
    app_password = unquote(url.password or "")
    db_name = url.path.lstrip("/")
    host, port = url.hostname or "localhost", url.port or 5432

    try:
        admin = psycopg.connect(host=host, port=port, user=admin_user, password=admin_password,
                                dbname="postgres", autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as e:
        print(f"[ERROR] Gagal masuk ke PostgreSQL sebagai '{admin_user}' di {host}:{port}: {str(e).strip()}")
        print("        Pastikan service PostgreSQL berjalan dan password superuser benar.")
        sys.exit(1)

    with admin:
        if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_user,)).fetchone():
            admin.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(app_user), sql.Literal(app_password)))
            print(f"[OK] User '{app_user}' sudah ada (password disamakan dengan config.py).")
        else:
            admin.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(app_user), sql.Literal(app_password)))
            print(f"[OK] User '{app_user}' dibuat.")

        if admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)).fetchone():
            print(f"[OK] Database '{db_name}' sudah ada.")
        else:
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(db_name), sql.Identifier(app_user)))
            print(f"[OK] Database '{db_name}' dibuat.")


def main():
    parser = argparse.ArgumentParser(description="Setup database lokal PostgreSQL Smart Parking Gate")
    parser.add_argument("--admin-user", default="postgres", help="Superuser PostgreSQL (default: postgres)")
    parser.add_argument("--admin-password", help="Password superuser (jika kosong akan ditanyakan)")
    args = parser.parse_args()

    print("=" * 65)
    print("  SETUP DATABASE LOKAL POSTGRESQL — GERBANG KELUAR FT UNTIRTA")
    print("=" * 65)
    admin_password = args.admin_password or getpass.getpass(
        f"Password superuser '{args.admin_user}' (diisi saat instalasi PostgreSQL): ")
    create_role_and_database(args.admin_user, admin_password)

    db = LocalDB()
    if not db.available:
        print("[ERROR] Tabel tidak dapat dibuat. Periksa LOCAL_DB_URL di config.py.")
        sys.exit(1)
    print("[OK] Tabel database lokal siap.")

    worker = SyncWorker(db, queue.Queue(), start_threads=False)
    if worker.enabled:
        print("[INFO] Menarik data awal dari Supabase...")
        if worker.sync_once():
            print("[OK] Sinkronisasi awal dengan Supabase berhasil.")
        else:
            print(f"[WARN] Sinkronisasi awal gagal ({worker.last_error}). Akan dicoba otomatis saat main.py berjalan.")

    print("-" * 65)
    print(f"  Akun petugas lokal   : {db.count_users()}")
    print(f"  Kendaraan terdaftar  : {len(db.list_vehicles())}")
    print(f"  Log belum terkirim   : {db.count_pending_logs()}")
    print("=" * 65)
    print("  Selesai. Jalankan sistem gerbang: python main.py")
    db.close()


if __name__ == "__main__":
    main()
