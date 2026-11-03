"""
Script CLI untuk mengelola pengguna (Users) di Supabase Cloud Database.
Dapat digunakan untuk menambah, melihat, dan menghapus user tanpa menyentuh kode HTML.

Penggunaan:
    # Melihat daftar user:
    python manage_users.py --list

    # Menambah user secara interaktif:
    python manage_users.py --add

    # Menambah user via argumen:
    python manage_users.py --add --name "Ahmad" --email "ahmad@untirta.ac.id" --password "rahasia123" --role "Petugas Gerbang"

    # Menghapus user berdasarkan email:
    python manage_users.py --delete ahmad@untirta.ac.id
"""

import sys
import argparse
import requests
import config


def get_headers():
    url = getattr(config, 'SUPABASE_URL', '').rstrip('/')
    key = getattr(config, 'SUPABASE_KEY', '')
    if not url or not key or 'YOUR_PROJECT_ID' in url:
        print("\n[ERROR] Silakan isi SUPABASE_URL dan SUPABASE_KEY di file config.py terlebih dahulu!")
        sys.exit(1)
    return url, {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }


def list_users():
    url, headers = get_headers()
    try:
        r = requests.get(f"{url}/rest/v1/users?select=*&order=id.asc", headers=headers, timeout=6)
        if r.status_code == 200:
            users = r.json()
            if not users:
                print("\n[INFO] Belum ada user di database Supabase.")
                return
            print("\n" + "=" * 70)
            print(f"  DAFTAR USER DI SUPABASE ({len(users)} Akun)")
            print("=" * 70)
            print(f"{'ID':<5} | {'NAMA':<20} | {'EMAIL':<30} | {'ROLE':<16}")
            print("-" * 70)
            for u in users:
                print(f"{u.get('id', ''):<5} | {u.get('name', ''):<20} | {u.get('email', ''):<30} | {u.get('role', ''):<16}")
            print("=" * 70 + "\n")
        else:
            print(f"[ERROR] Gagal mengambil user (status {r.status_code}): {r.text}")
    except Exception as e:
        print(f"[ERROR] Koneksi ke Supabase gagal: {e}")


def add_user(name=None, email=None, password=None, role=None):
    url, headers = get_headers()
    
    # Mode interaktif jika argumen kosong
    if not name:
        print("\n=== TAMBAH USER BARU KE SUPABASE ===")
        name = input("Nama Lengkap: ").strip()
    if not email:
        email = input("Email (@untirta.ac.id): ").strip().lower()
    if not password:
        password = input("Password: ").strip()
    if not role:
        print("Pilihan Role: [1] Admin  [2] Petugas Gerbang  [3] Operator")
        pilihan = input("Pilih Role (1/2/3, default 2): ").strip()
        role = "Admin" if pilihan == "1" else "Operator" if pilihan == "3" else "Petugas Gerbang"

    if not name or not email or not password:
        print("[ERROR] Nama, email, dan password wajib diisi!")
        return

    payload = {
        "name": name,
        "email": email.lower(),
        "password": password,
        "role": role
    }

    try:
        r = requests.post(f"{url}/rest/v1/users", headers=headers, json=payload, timeout=6)
        if r.status_code in (200, 201):
            print(f"\n[SUKSES] Akun '{name}' ({role}) berhasil ditambahkan ke Supabase!")
        elif r.status_code == 409 or 'duplicate' in r.text.lower():
            print(f"\n[GAGAL] Email '{email}' sudah terdaftar di database.")
        else:
            print(f"\n[ERROR] Status {r.status_code}: {r.text}")
    except Exception as e:
        print(f"\n[ERROR] Gagal menghubungi Supabase: {e}")


def delete_user(email):
    if not email:
        email = input("Masukkan email user yang ingin dihapus: ").strip().lower()

    url, headers = get_headers()
    confirm = input(f"Yakin ingin menghapus user dengan email '{email}'? (y/N): ").strip().lower()
    if confirm != 'y':
        print("Penghapusan dibatalkan.")
        return

    try:
        r = requests.delete(f"{url}/rest/v1/users?email=eq.{email}", headers=headers, timeout=6)
        if r.status_code in (200, 204):
            print(f"\n[SUKSES] User '{email}' berhasil dihapus dari Supabase.")
        else:
            print(f"\n[ERROR] Status {r.status_code}: {r.text}")
    except Exception as e:
        print(f"\n[ERROR] Gagal menghapus user: {e}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Pengelola User Supabase ALPR FT Untirta")
    parser.add_argument('--list', action='store_true', help="Tampilkan daftar semua user")
    parser.add_argument('--add', action='store_true', help="Tambah user baru")
    parser.add_argument('--delete', type=str, nargs='?', const='', help="Hapus user berdasarkan email")
    parser.add_argument('--name', type=str, help="Nama lengkap user")
    parser.add_argument('--email', type=str, help="Email user")
    parser.add_argument('--password', type=str, help="Password user")
    parser.add_argument('--role', type=str, help="Role user (Admin / Petugas Gerbang / Operator)")

    args = parser.parse_args()

    if args.list:
        list_users()
    elif args.add:
        add_user(args.name, args.email, args.password, args.role)
    elif args.delete is not None:
        delete_user(args.delete)
    else:
        # Default jika tanpa argumen: tampilkan menu sederhana
        print("\n=== PENGELOLA USER SUPABASE — ALPR FT UNTIRTA ===")
        print("1. Lihat Daftar User (--list)")
        print("2. Tambah User Baru (--add)")
        print("3. Hapus User (--delete)")
        p = input("Pilih menu (1/2/3): ").strip()
        if p == '1':
            list_users()
        elif p == '2':
            add_user()
        elif p == '3':
            delete_user('')
        else:
            parser.print_help()

