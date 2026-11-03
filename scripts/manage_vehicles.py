"""
Script CLI untuk mengelola Kendaraan Terdaftar (Registered Vehicles) & Log Deteksi di Supabase.
Mendukung pendaftaran plat nomor kendaraan dan UID kartu RFID 13.56 MHz.

Penggunaan:
    # Melihat daftar kendaraan & kartu civitas yang terdaftar:
    python scripts/manage_vehicles.py --list

    # Menambah kendaraan secara interaktif:
    python scripts/manage_vehicles.py --add

    # Menambah kendaraan via argumen:
    python scripts/manage_vehicles.py --add --plate "A 1234 BC" --name "Ahmad Zaki" --category "STUDENT" --nim "3332210001" --rfid "0001234567"

    # Menghapus kendaraan terdaftar:
    python scripts/manage_vehicles.py --delete "A 1234 BC"

    # Melihat riwayat log deteksi gerbang terbaru:
    python scripts/manage_vehicles.py --logs
"""

import os
import sys
import argparse
import requests
# Folder utama proyek ke path agar config & smart_gate dapat di-import
# (bisa dijalankan: python scripts/<nama>.py  atau  python -m scripts.<nama>)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def list_vehicles():
    url, headers = get_headers()
    try:
        r = requests.get(f"{url}/rest/v1/registered_vehicles?select=*&order=id.asc", headers=headers, timeout=6)
        if r.status_code == 200:
            vehicles = r.json()
            if not vehicles:
                print("\n[INFO] Belum ada kendaraan terdaftar di Supabase.")
                return
            print("\n" + "=" * 98)
            print(f"  DAFTAR KENDARAAN & RFID TERDAFTAR (CIVITAS AKADEMIKA) — Total: {len(vehicles)}")
            print("=" * 98)
            print(f"{'ID':<4} | {'PLAT NOMOR':<13} | {'NAMA PEMILIK':<22} | {'KATEGORI':<18} | {'NIM / NIP':<15} | {'RFID UID':<15}")
            print("-" * 98)
            for v in vehicles:
                owner = v.get('owner') or v.get('owner_name', '')
                rfid_val = v.get('rfid_uid') or 'AUTO-BIND (NULL)'
                print(f"{v.get('id', ''):<4} | {v.get('plate', ''):<13} | {owner:<22} | {v.get('category', ''):<18} | {v.get('nim_nip', '-'):<15} | {rfid_val:<15}")
            print("=" * 98 + "\n")
        else:
            print(f"[ERROR] Gagal mengambil data (status {r.status_code}): {r.text}")
    except Exception as e:
        print(f"[ERROR] Koneksi ke Supabase gagal: {e}")


def add_vehicle(plate=None, name=None, category=None, nim_nip=None, rfid=None):
    url, headers = get_headers()
    
    # Mode interaktif jika argumen belum lengkap
    if not plate:
        print("\n=== REGISTRASI KENDARAAN CIVITAS BARU ===")
        plate = input("Plat Nomor (contoh: B 1234 ABC): ").strip().upper()
    else:
        plate = plate.strip().upper()

    if not name:
        name = input("Nama Pemilik: ").strip()

    if not category:
        print("Pilihan Kategori: [1] STUDENT  [2] LECTURER  [3] ACADEMIC COMMUNITY")
        pilihan = input("Pilih Kategori (1/2/3, default 1): ").strip()
        category = "LECTURER" if pilihan == "2" else "ACADEMIC COMMUNITY" if pilihan == "3" else "STUDENT"
    else:
        cat_upper = category.strip().upper()
        if cat_upper in ('STUDENT', 'LECTURER', 'ACADEMIC COMMUNITY'):
            category = cat_upper
        else:
            category = 'STUDENT'

    if nim_nip is None:
        nim_nip = input("NIM / NIP (opsional, tekan enter jika kosong): ").strip() or "-"

    if rfid is None:
        rfid = input("UID Kartu RFID USB (kosongkan untuk Auto-Bind saat tap pertama di gerbang): ").strip() or None
    else:
        rfid = rfid.strip() or None

    if not plate or not name:
        print("[ERROR] Plat nomor dan nama pemilik wajib diisi!")
        return

    payload = {
        "plate": plate,
        "owner": name,
        "category": category,
        "nim_nip": nim_nip,
        "rfid_uid": rfid
    }

    try:
        r = requests.post(f"{url}/rest/v1/registered_vehicles", headers=headers, json=payload, timeout=6)
        if r.status_code in (200, 201):
            mode_str = f"RFID: {rfid}" if rfid else "Mode: AUTO-BIND saat tap pertama di gerbang"
            print(f"\n[SUKSES] Plat '{plate}' atas nama '{name}' ({category} | {mode_str}) berhasil didaftarkan!")
        elif r.status_code == 409 or 'duplicate' in r.text.lower():
            print(f"\n[GAGAL] Plat '{plate}' atau RFID UID sudah terdaftar di database.")
        else:
            print(f"\n[ERROR] Status {r.status_code}: {r.text}")
    except Exception as e:
        print(f"\n[ERROR] Gagal menghubungi Supabase: {e}")


def delete_vehicle(plate):
    if not plate:
        plate = input("Masukkan plat nomor kendaraan yang ingin dihapus: ").strip().upper()
    else:
        plate = plate.strip().upper()

    url, headers = get_headers()
    confirm = input(f"Yakin ingin menghapus kendaraan plat '{plate}'? (y/N): ").strip().lower()
    if confirm != 'y':
        print("Penghapusan dibatalkan.")
        return

    try:
        r = requests.delete(f"{url}/rest/v1/registered_vehicles?plate=eq.{plate}", headers=headers, timeout=6)
        if r.status_code in (200, 204):
            print(f"\n[SUKSES] Kendaraan dengan plat '{plate}' berhasil dihapus.")
        else:
            print(f"\n[ERROR] Status {r.status_code}: {r.text}")
    except Exception as e:
        print(f"\n[ERROR] Gagal menghapus kendaraan: {e}")


def show_logs(limit=15):
    url, headers = get_headers()
    try:
        r = requests.get(f"{url}/rest/v1/vehicle_logs?select=*&order=created_at.desc&limit={limit}", headers=headers, timeout=6)
        if r.status_code == 200:
            logs = r.json()
            if not logs:
                print("\n[INFO] Belum ada log deteksi gerbang di Supabase.")
                return
            print("\n" + "=" * 105)
            print(f"  RIWAYAT DETEKSI GERBANG TERBARU (VEHICLE LOGS) — Terakhir {len(logs)} Kejadian")
            print("=" * 105)
            print(f"{'WAKTU':<19} | {'METODE':<7} | {'PLAT / UID':<14} | {'NAMA PEMILIK':<22} | {'KATEGORI':<18} | {'STATUS':<15}")
            print("-" * 105)
            for l in logs:
                waktu = l.get('created_at', '')[:19].replace('T', ' ')
                metode = l.get('method', 'ALPR')
                print(f"{waktu:<19} | {metode:<7} | {l.get('plate', ''):<14} | {l.get('name', ''):<22} | {l.get('category', ''):<18} | {l.get('status', 'TERDAFTAR'):<15}")
            print("=" * 105 + "\n")
        else:
            print(f"[ERROR] Gagal mengambil log (status {r.status_code}): {r.text}")
    except Exception as e:
        print(f"[ERROR] Koneksi ke Supabase gagal: {e}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Pengelola Kendaraan & RFID Terdaftar ALPR FT Untirta")
    parser.add_argument('--list', action='store_true', help="Tampilkan daftar kendaraan terdaftar")
    parser.add_argument('--add', action='store_true', help="Tambah kendaraan terdaftar baru")
    parser.add_argument('--delete', type=str, nargs='?', const='', help="Hapus kendaraan berdasarkan plat")
    parser.add_argument('--logs', action='store_true', help="Tampilkan log deteksi gerbang terbaru")
    parser.add_argument('--plate', type=str, help="Plat nomor kendaraan (contoh: B 1234 ABC)")
    parser.add_argument('--name', type=str, help="Nama pemilik kendaraan")
    parser.add_argument('--category', type=str, help="Kategori (STUDENT / LECTURER / ACADEMIC COMMUNITY)")
    parser.add_argument('--nim', type=str, help="NIM atau NIP pemilik")
    parser.add_argument('--rfid', type=str, help="UID Kartu RFID USB (contoh: 0001234567)")

    args = parser.parse_args()

    if args.list:
        list_vehicles()
    elif args.add:
        add_vehicle(args.plate, args.name, args.category, args.nim, args.rfid)
    elif args.delete is not None:
        delete_vehicle(args.delete)
    elif args.logs:
        show_logs()
    else:
        print("\n=== PENGELOLA KENDARAAN TERDAFTAR (SUPABASE) ===")
        print("1. Lihat Daftar Kendaraan & RFID (--list)")
        print("2. Tambah Kendaraan Baru (--add)")
        print("3. Hapus Kendaraan Terdaftar (--delete)")
        print("4. Lihat Log Deteksi Gerbang Terbaru (--logs)")
        p = input("Pilih menu (1/2/3/4): ").strip()
        if p == '1':
            list_vehicles()
        elif p == '2':
            add_vehicle()
        elif p == '3':
            delete_vehicle('')
        elif p == '4':
            show_logs()
        else:
            parser.print_help()
