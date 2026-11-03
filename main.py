"""
Sistem Smart Parking Gate FT UNTIRTA (EXIT GATE — GERBANG KELUAR)
Platform: Mini PC (Pusat Pemrosesan) + ESP32 (Kontrol I/O Gerbang via USB Serial)

Sistem Verifikasi Ganda (Dual-Factor Authentication):
- KEDUA PARAMETER (PLAT NOMOR DAN KARTU RFID) HARUS COCOK dengan database!
- Jika plat cocok dan kartu RFID cocok milik kendaraan yang sama -> Palang KELUAR terbuka.
- Jika plat terdaftar tetapi kartu salah/berbeda -> Palang TETAP TERTUTUP (Alarm bunyi).
- Jika kendaraan belum terdaftar (Tamu) -> Petugas verifikasi manual lalu tekan Push Button.

Komponen Terintegrasi:
  Mini PC (USB):
  1. Mini PC (Pusat Kontrol: ALPR, Database, Web Stream)
  2. Webcam USB (Perekam Plat Keluar)
  3. RFID USB 13.56 MHz (Pembaca Kartu Civitas)
  4. ESP32 DevKit (USB Serial)
  ESP32 (GPIO):
  5. Buzzer Flash AD16-22SM 12V (GPIO26 via Transistor NPN)
  6. Motor Servo Palang (GPIO13)
  7. LCD I2C 20x4 (SDA: GPIO21, SCL: GPIO22)
  8. Push Button Manual (GPIO27)
  9. Dual Sensor Infrared E18-D80NK:
     - Sensor IR 1 (Sebelum Palang): GPIO32 (Deteksi Kedatangan / ALPR)
     - Sensor IR 2 (Setelah Palang): GPIO33 (Pemicu Penutup Palang)
"""

import os
import re
import sys
import time
import argparse

# Harus diset SEBELUM cv2 di-import: tanpa ini backend MSMF butuh ~30 detik untuk membuka webcam
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import cv2
import numpy as np

import config
from smart_gate.vision.camera import CameraStream
from smart_gate.hardware.hardware import HardwareManager
from smart_gate.vision.plate_recognizer import PlateRecognizerAPI, draw_results
from smart_gate.database.gate_database import GateDatabase
from smart_gate.web.stream_server import VideoStreamServer
import threading


class SmartParkingExitGate:
    def __init__(self):
        print("=" * 65)
        print("  SMART PARKING GATE FT UNTIRTA — GERBANG KELUAR (EXIT ONLY)")
        print("  Sistem Verifikasi: DUAL AUTH (PLAT DAN RFID HARUS MATCH!)")
        print("  Bypass Tamu: Push Button Manual Petugas")
        print("  Database: POSTGRESQL LOKAL + Sinkron SUPABASE (Arah: OUT / Keluar)")
        print("=" * 65)

        # 1. Inisialisasi Perangkat Keras
        self.hw = HardwareManager()

        # 2. Inisialisasi Database Lokal (+ sinkron Supabase di background) & AI API
        self.db = GateDatabase()
        self.api = PlateRecognizerAPI()

        # 3. Inisialisasi Live Web Stream Server (Port 5050)
        self.stream_server = None
        if getattr(config, 'STREAM_ENABLED', True):
            self.stream_server = VideoStreamServer(
                port=getattr(config, 'STREAM_PORT', 5050),
                enable_tunnel=getattr(config, 'CLOUDFLARE_TUNNEL_ENABLED', True),
                width=getattr(config, 'STREAM_WIDTH', 640),
                quality=getattr(config, 'STREAM_JPEG_QUALITY', 60),
                max_fps=getattr(config, 'STREAM_MAX_FPS', 0),
                db=self.db
            )
            self.stream_server.start()

        # 4. Listener Tombol Terminal VSCode (Spasi, M, Q)
        self.terminal_trigger = False
        self.terminal_manual = False
        self.terminal_quit = False
        self.stop_terminal_listener = False
        self._start_terminal_listener()

        # 5. Inisialisasi Kamera USB (thread terpisah: live stream tetap berjalan saat verifikasi)
        self.camera = CameraStream(
            on_frame=self.stream_server.update_frame if self.stream_server else None
        )

        if config.SAVE_RESULTS:
            os.makedirs(config.OUTPUT_DIR, exist_ok=True)

        # Standby gerbang keluar
        self.hw.lcd.display(
            " GERBANG KELUAR FT  ",
            "  UNTIRTA CILEGON   ",
            "SISTEM DUAL-AUTH OK ",
            "PALANG TERTUTUP (0*)"
        )
        self.hw.close_gate()
        self.hw.buzzer_beep(0.15, 1)
        time.sleep(1.5)
        self.hw.lcd.display_standby()

        print("[OK] Gerbang keluar siap beroperasi dengan verifikasi PLAT + RFID!\n")

    def _start_terminal_listener(self):
        """
        Membaca tombol keyboard (Spasi, M, Q) langsung di Terminal VSCode.
        Ketikan RFID USB (mode keyboard) yang masuk ke terminal diteruskan ke buffer RFID.
        """
        if not sys.platform.startswith('win'):
            return

        def _terminal_loop():
            import msvcrt
            while not self.stop_terminal_listener:
                try:
                    if msvcrt.kbhit():
                        ch = msvcrt.getch()
                        if ch in (b'\x00', b'\xe0'):  # Tombol khusus (panah/F1..), abaikan kode keduanya
                            msvcrt.getch()
                            continue
                        if self.hw.feed_keyboard_char(ch.decode('latin-1')):
                            continue
                        if ch in (b' ', b'\r'):  # Tombol SPASI atau ENTER ditekan di terminal VSCode
                            self.terminal_trigger = True
                        elif ch in (b'm', b'M'):  # Tombol M (Buka Manual)
                            self.terminal_manual = True
                        elif ch in (b'q', b'Q', b'\x1b'):  # Tombol Q atau ESC
                            self.terminal_quit = True
                            break
                    time.sleep(0.04)
                except Exception:
                    break

        t = threading.Thread(target=_terminal_loop, daemon=True)
        t.start()

    def _wait_key(self, delay_ms: int) -> int:
        """cv2.waitKey yang meneruskan ketikan RFID USB (mode keyboard) ke buffer RFID."""
        key = cv2.waitKey(delay_ms) & 0xFF
        if key != 255 and self.hw.feed_keyboard_char(chr(key)):
            return 255
        return key

    def _show_for(self, seconds: float):
        """Tahan tampilan jendela kamera tanpa menghilangkan ketikan RFID yang masuk."""
        end = time.time() + seconds
        while time.time() < end:
            self._wait_key(20)

    def _fit_preview(self, frame):
        """Salinan frame yang diperkecil untuk jendela preview (frame 1080p lebih besar dari layar)."""
        max_w = getattr(config, 'PREVIEW_WIDTH', 960)
        h, w = frame.shape[:2]
        if w <= max_w:
            return frame.copy()
        return cv2.resize(frame, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)

    def capture_frame(self):
        """Ambil 1 frame citra terbaru dari webcam USB (diambil setelah fungsi dipanggil)."""
        return self.camera.read_fresh()

    def handle_exit_success(self, info: dict):
        """Aksi saat KEDUA PARAMETER (Plat + RFID) COCOK atau AUTO-BIND Berhasil."""
        plate = info.get("plate", "-")
        owner = info.get("owner_name", "Civitas")
        cat = info.get("category", "STUDENT")
        auto_bound = info.get("auto_bound", False)

        if auto_bound:
            print(f"\n>>> [AUTO-BIND RFID & AKSES KELUAR DITERIMA \u2705] Kartu '{info.get('rfid_uid')}' resmi terikat ke {owner} ({plate})!")
            self.hw.lcd.display(
                "*AKTIVASI KARTU OK!*",
                f"{plate} ({cat[:7]})",
                f"{owner[:20]}",
                "HATI-HATI DI JALAN:)"
            )
        else:
            print(f"\n>>> [AKSES KELUAR DITERIMA \u2705] PLAT & RFID COCOK: {owner} ({plate})")
            self.hw.lcd.display(
                "*** AKSES KELUAR ***",
                f"{plate} ({cat[:7]})",
                f"{owner[:20]}",
                "HATI-HATI DI JALAN:)"
            )

        # 2. Bunyikan Buzzer 2x
        self.hw.buzzer_beep(0.12, 2)

        # 3. Buka Palang Gerbang Keluar (90 Derajat)
        self.hw.open_gate()

        # 4. Kirim Data Lengkap ke Supabase (vehicle_logs)
        self.db.log_vehicle(
            plate_text=plate,
            confidence=info.get("confidence", 95.0),
            direction="OUT",
            method="ALPR+RFID",
            custom_info=info
        )

        # 5. Keamanan & Penutup Palang Otomatis (Sensor IR 2 + Safety Timer)
        self.wait_and_close_gate()

    def handle_exit_failed(self, info: dict, reason: str = "TIDAK COCOK"):
        """Aksi saat verifikasi keluar gagal (Salah Kartu / Belum Terdaftar)."""
        plate = info.get("plate", "TIDAK DIKENAL")
        rfid = info.get("rfid_uid", "-")
        status = info.get("status", "TIDAK TERDAFTAR")

        if reason == "RFID_MISMATCH":
            print(f"\n>>> [AKSES DITOLAK \u26a0\ufe0f] KARTU TIDAK SESUAI DENGAN PLAT: {plate} vs RFID {rfid}")
            self.hw.lcd.display(
                "! DATA TIDAK COCOK !",
                f"PLAT: {plate[:14]}",
                "KARTU BUKAN MILIKNYA",
                "MOHON LAPOR SATPAM  "
            )
        elif reason == "DB_OFFLINE":
            print(f"\n>>> [AKSES DITOLAK \u26a0\ufe0f] DATABASE LOKAL TIDAK DAPAT DIAKSES: {plate}")
            self.hw.lcd.display(
                "! DATABASE OFFLINE !",
                f"PLAT: {plate[:14]}",
                "VERIFIKASI GAGAL    ",
                "MOHON LAPOR PETUGAS "
            )
        else:
            print(f"\n>>> [AKSES DITOLAK \u26a0\ufe0f] BELUM TERDAFTAR: {plate}")
            self.hw.lcd.display(
                "** BELUM TERDAFTAR *",
                f"{plate[:20]}",
                "VERIFIKASI GAGAL    ",
                "MOHON LAPOR PETUGAS "
            )

        # Alarm Buzzer Strobo Panjang
        self.hw.buzzer_alert()

        # Palang TETAP TERTUTUP demi keamanan
        self.hw.close_gate()

        # Catat kegagalan ke database lokal (disinkron ke Supabase)
        self.db.log_vehicle(
            plate_text=plate,
            confidence=info.get("confidence", 80.0),
            direction="OUT",
            method="ALPR+RFID",
            custom_info=info
        )

        time.sleep(3.0)
        self.hw.lcd.display_standby()

    def handle_manual_override(self):
        """Buka gerbang keluar manual via Push Button oleh Petugas (Tamu/Belum Terdaftar)."""
        print("\n>>> [MANUAL KELUAR] Tombol ditekan oleh petugas! Membuka gerbang keluar...")
        self.hw.lcd.display(
            "** MANUAL KELUAR ** ",
            "PETUGAS BUKA GERBANG",
            "PALANG DIBUKA       ",
            "SILAKAN MELINTAS... "
        )
        self.hw.buzzer_beep(0.2, 1)
        self.hw.open_gate()

        # Catat log keluar manual ke database lokal (disinkron ke Supabase)
        manual_info = {
            "matched": True,
            "plate": "MANUAL-EXIT",
            "owner_name": "Tamu (Buka Manual)",
            "category": "ACADEMIC COMMUNITY",
            "nim_nip": "-",
            "status": "TERDAFTAR",
            "confidence": 100.0
        }
        self.db.log_vehicle(
            plate_text="MANUAL-EXIT",
            confidence=100.0,
            direction="OUT",
            method="MANUAL",
            custom_info=manual_info
        )

        # Keamanan & Penutup Palang Otomatis (Sensor IR 2 + Safety Timer)
        self.wait_and_close_gate()

    def handle_web_remote_override(self, cmd_info: dict):
        """
        Buka gerbang keluar via Web Dashboard Petugas (lokal :5050 langsung, atau Netlify via Supabase).
        Log hanya dicatat di sini (saat palang benar-benar dibuka), bukan oleh dashboard.
        """
        operator = cmd_info.get("operator_name") or "Petugas Web"
        source = "DASHBOARD LOKAL" if cmd_info.get("source") == "LOCAL" else "NETLIFY"
        print(f"\n>>> [REMOTE WEB KELUAR] Perintah buka palang diterima dari {source} oleh: {operator}!")
        self.hw.lcd.display(
            "** REMOTE BUKA WEB *",
            f"PETUGAS: {operator[:11]}",
            "PALANG DIBUKA (90*) ",
            "SILAKAN MELINTAS... "
        )
        self.hw.buzzer_beep(0.2, 1)
        self.hw.open_gate()

        # Plat target: dari dashboard lokal (field plate) atau dari Netlify (format alasan "... [PLAT]")
        plate = (cmd_info.get("plate") or "").strip()
        if not plate:
            m = re.search(r"\[([^\]]+)\]\s*$", cmd_info.get("reason") or "")
            plate = m.group(1).strip() if m else ""
        vehicle = self.db.lookup_vehicle(plate) if plate and plate != "MANUAL-WEB" else {"matched": False}

        if vehicle.get("matched"):
            manual_info = {**vehicle, "confidence": 100.0}
        else:
            manual_info = {
                "matched": True,
                "plate": plate or "MANUAL-WEB",
                "owner_name": f"Tamu (Web: {operator})",
                "category": "ACADEMIC COMMUNITY",
                "nim_nip": "-",
                "status": "TERDAFTAR",
                "confidence": 100.0
            }
        self.db.log_vehicle(
            plate_text=manual_info["plate"],
            confidence=100.0,
            direction="OUT",
            method="WEB_MANUAL",
            custom_info=manual_info
        )

        # Keamanan & Penutup Palang Otomatis (Sensor IR 2 + Safety Timer)
        self.wait_and_close_gate()

    def wait_and_close_gate(self):
        """
        Logika Penutup Palang Otomatis Berbasis 2 Sensor Infrared (Bebas Bug):
        - Sensor IR 1 (Sebelum Palang): Memastikan kendaraan mulai maju.
        - Sensor IR 2 (Setelah Palang): Mendeteksi kendaraan melintas melewati palang.

        Alur Eksekusi Tanpa Bug:
        1. Tunggu kendaraan bergerak maju & memicu Sensor IR 2 (ESP32 melaporkan IR2 = 1).
           Jika kendaraan mogok/batal melintas hingga GATE_MAX_WAIT detik, palang ditutup fail-safe.
        2. Tunggu kendaraan SELESAI melintasi Sensor IR 2 (ESP32 melaporkan IR2 = 0).
           Mencegah palang turun saat bodi kendaraan masih berada di bawah palang/sensor.
        3. Mulai timer jeda GATE_CLOSE_DELAY (misal 2.0 detik) sebelum palang menutup.
           PROTEKSI RE-TRIGGER: Jika Sensor IR 2 terhalang kembali selama countdown
           (misal kendaraan panjang / ada mobil lain menempel), timer otomatis DIRESET
           dan kembali menunggu sensor bebas agar palang tidak pernah menimpa kendaraan!
        4. Setelah timer selesai dan aman, buzzer berbunyi singkat & palang ditutup ke 0 derajat.
        """
        if not self.hw.is_connected:
            # Mode Simulasi (ESP32 belum terhubung)
            print("  [SIMULASI] Menunggu kendaraan melaju menuju Sensor IR 2 (Setelah Palang)...")
            time.sleep(1.2)
            print("  [SIMULASI] Sensor IR 2 TERDETEKSI (Kendaraan sedang melintas di bawah palang).")
            time.sleep(1.2)
            print("  [SIMULASI] Sensor IR 2 BEBAS (Kendaraan telah melewati palang).")
            print(f"  [SIMULASI] Menjalankan safety timer {config.GATE_CLOSE_DELAY} detik sebelum menutup palang...")
            time.sleep(config.GATE_CLOSE_DELAY)
            self.hw.buzzer_beep(0.1, 1)
            self.hw.close_gate()
            print("  [GERBANG] Palang berhasil ditutup kembali (0 derajat).")
            time.sleep(1.0)
            self.hw.lcd.display_standby()
            return

        start_time = time.time()
        max_wait = getattr(config, 'GATE_MAX_WAIT', 25.0)
        close_delay = getattr(config, 'GATE_CLOSE_DELAY', 2.0)

        print("\n  [SAFETY GATE] Palang terbuka. Menunggu kendaraan menuju Sensor IR 2 (Setelah Palang)...")
        self.hw.lcd.display(
            "  SILAKAN MELINTAS  ",
            "   HATI-HATI JALAN  ",
            "MENUJU SENSOR 2...  ",
            "PALANG TERBUKA (90*)"
        )

        # -----------------------------------------------------------------
        # TAHAP 1: Tunggu kendaraan menyentuh/memicu Sensor IR 2
        # -----------------------------------------------------------------
        sensor2_triggered = False
        while time.time() - start_time < max_wait:
            if self.hw.is_vehicle_at_sensor2():
                sensor2_triggered = True
                print("  [IR 2 AKTIF \u2705] Kendaraan terdeteksi melintasi Sensor IR 2!")
                break
            time.sleep(0.05)

        # Kasus Fail-safe: Kendaraan tidak pernah mencapai Sensor 2 dalam GATE_MAX_WAIT detik
        if not sensor2_triggered:
            if not self.hw.is_vehicle_at_sensor1():
                print(f"  [FAIL-SAFE] Timeout {max_wait}s: Kendaraan batal melintas / sudah pergi. Menutup palang.")
            else:
                print(f"  [FAIL-SAFE] Timeout {max_wait}s: Menutup palang demi keamanan.")
            self.hw.close_gate()
            time.sleep(1.0)
            self.hw.lcd.display_standby()
            return

        # -----------------------------------------------------------------
        # TAHAP 2: Tunggu kendaraan SELESAI melintasi Sensor IR 2
        # (Sensor 2 harus kembali HIGH / tidak terhalang)
        # -----------------------------------------------------------------
        print("  [IR 2 PROSES] Kendaraan sedang melintas. Menunggu seluruh bodi melewati Sensor 2...")
        self.hw.lcd.display(
            " KENDARAAN MELINTAS ",
            "   JANGAN BERHENTI  ",
            "MELEWATI SENSOR 2...",
            "PALANG TERBUKA (90*)"
        )

        stuck_timeout = 25.0
        stuck_start = time.time()
        while self.hw.is_vehicle_at_sensor2():
            if time.time() - stuck_start > stuck_timeout:
                print("  [WARN] Kendaraan terdiam di atas Sensor IR 2 lebih dari 25s!")
                break
            time.sleep(0.1)

        print("  [IR 2 CLEAR \u2705] Kendaraan telah selesai melewati Sensor IR 2.")

        # -----------------------------------------------------------------
        # TAHAP 3: Safety Countdown Timer sebelum palang menutup
        # Dengan re-trigger protection: jika Sensor 2 terhalang lagi, reset timer!
        # -----------------------------------------------------------------
        print(f"  [SAFETY TIMER] Memulai timer penutup {close_delay} detik...")
        
        timer_remaining = close_delay
        interval = 0.1
        while timer_remaining > 0:
            # Re-trigger protection: Jika ada kendaraan susulan / objek terdeteksi lagi di IR 2
            if self.hw.is_vehicle_at_sensor2():
                print("  [RE-TRIGGER \u26a0\ufe0f] Sensor IR 2 kembali terhalang! Mereset safety timer...")
                self.hw.lcd.display(
                    "OBJEK TERDETEKSI IR2",
                    "MENUNGGU BEBAS...   ",
                    "TIMER DIRESET       ",
                    "PALANG TETAP TERBUKA"
                )
                while self.hw.is_vehicle_at_sensor2():
                    time.sleep(0.1)
                timer_remaining = close_delay # Reset timer penuh
                continue

            self.hw.lcd.display(
                "  KENDARAAN LEWAT   ",
                " SENSOR 2 CLEAR OK  ",
                "PALANG AKAN MENUTUP ",
                f"WAKTU: {timer_remaining:.1f} DETIK"
            )
            time.sleep(interval)
            timer_remaining -= interval

        # -----------------------------------------------------------------
        # TAHAP 4: Tutup Palang
        # -----------------------------------------------------------------
        self.hw.buzzer_beep(0.1, 1)
        self.hw.close_gate()
        print("  [GERBANG] Palang berhasil ditutup kembali (0 derajat).")

        self.hw.lcd.display(
            " GERBANG KELUAR FT  ",
            "  UNTIRTA CILEGON   ",
            "  PALANG TERTUTUP   ",
            "    TERIMA KASIH    "
        )
        time.sleep(1.5)

        # Tunggu sampai Sensor 1 benar-benar bebas sebelum siap menerima kendaraan baru
        debounce_start = time.time()
        while self.hw.is_vehicle_at_sensor1() and (time.time() - debounce_start < 4.0):
            time.sleep(0.2)

        self.hw.lcd.display_standby()

    def process_dual_verification(self, initial_rfid: str = None, frame = None):
        """
        Alur Verifikasi Ganda:
        1. Baca Plat Nomor dari Kamera USB.
        2. Minta Pengendara Tap Kartu RFID (atau gunakan initial_rfid jika tap duluan).
        3. Cocokkan: KEDUA parameter (Plat & RFID) HARUS sesuai dengan data yang sama di database.
        """
        print("\n[PROSES VERIFIKASI GANDA] Kendaraan terdeteksi di gerbang keluar...")
        self.hw.lcd.display(
            "KENDARAAN KELUAR... ",
            "MENGAMBIL FOTO PLAT ",
            "MEMPROSES OCR API...",
            "MOHON TUNGGU...     "
        )
        self.hw.buzzer_beep(0.08, 1)

        # 1. Ambil foto dan deteksi plat via Webcam USB
        if frame is None:
            frame = self.capture_frame()
        if frame is None:
            print("[ERROR] Gagal membaca webcam!")
            self.hw.lcd.display(
                "  GAGAL AMBIL FOTO  ",
                "PERIKSA WEBCAM USB  ",
                "TEKAN TOMBOL MANUAL ",
                "HUBUNGI PETUGAS     "
            )
            time.sleep(2.0)
            self.hw.lcd.display_standby()
            return

        # Simpan snapshot mentah untuk memudahkan pengecekan hasil pemotretan
        if config.SAVE_RESULTS:
            cv2.imwrite(os.path.join(config.OUTPUT_DIR, "latest_capture.jpg"), frame)

        api_res = self.api.recognize(frame)
        if not api_res.get("success"):
            err_msg = api_res.get("error") or "Koneksi ke Plate Recognizer API gagal"
            print(f"  [API ERROR] {err_msg}")
            self.hw.lcd.display(
                " GAGAL KONEKSI OCR  ",
                "PERIKSA INTERNET    ",
                "TEKAN TOMBOL MANUAL ",
                "HUBUNGI PETUGAS     "
            )
            time.sleep(2.0)
            self.hw.lcd.display_standby()
            return

        if not api_res.get("results"):
            print("  [--] Tidak ada plat nomor terdeteksi pada citra. Pastikan plat menghadap kamera & pencahayaan cukup.")
            self.hw.lcd.display(
                "PLAT TIDAK TERBACA  ",
                "DEKATKAN KENDARAAN  ",
                "ATAU TEKAN TOMBOL   ",
                "MANUAL OLEH PETUGAS "
            )
            time.sleep(2.0)
            self.hw.lcd.display_standby()
            return

        best = api_res["results"][0]
        plate_text = best.get("plate_text", "")
        conf = best.get("confidence", 0.0)

        if config.SAVE_RESULTS:
            annotated = draw_results(frame.copy(), [best])
            fname = os.path.join(config.OUTPUT_DIR, f"exit_{int(time.time())}.jpg")
            cv2.imwrite(fname, annotated)

        print(f"  [ALPR OK \u2705] Plat terdeteksi: {plate_text} (Akurasi: {conf:.0%})")

        # Tampilkan visual deteksi plat pada jendela kamera
        if getattr(config, 'SHOW_PREVIEW', True):
            annotated_disp = self._fit_preview(draw_results(frame.copy(), [best]))
            cv2.imshow("Smart Parking Gate ALPR - FT Untirta", annotated_disp)
            self._show_for(0.8)

        # 2. Ambil Kartu RFID (Cek juga apakah plat sedang menunggu Auto-Bind kartu baru)
        veh_precheck = self.db.lookup_vehicle(plate_text)
        needs_bind = bool(veh_precheck.get("matched") and veh_precheck.get("needs_rfid_bind"))
        if needs_bind:
            print(f"  [AUTO-BIND SIAP \u26a1] Plat '{plate_text}' ({veh_precheck.get('owner_name')}) terdaftar mandiri & menunggu aktivasi kartu RFID pertama!")

        rfid_uid = initial_rfid
        if not rfid_uid:
            # Pengendara belum tap kartu, minta tap sekarang
            timeout = getattr(config, 'RFID_WAIT_TIMEOUT', 12.0)
            start_wait = time.time()
            self.hw.buzzer_beep(0.1, 1)

            print(f"  [MENUNGGU RFID] Menunggu pengendara tap kartu (maks {timeout} detik)...")
            if not self.hw.is_connected:
                print("  [SIMULASI PC] Tekan [SPASI] di jendela kamera untuk simulasi Tap RFID.")

            while time.time() - start_wait < timeout:
                remaining = int(timeout - (time.time() - start_wait))
                if needs_bind:
                    self.hw.lcd.display(
                        f"PLAT: {plate_text[:14]}",
                        "AKTIVASI KARTU BARU!",
                        "TAP RFID KOSONG ANDA",
                        f"WAKTU: {remaining} DETIK"
                    )
                else:
                    self.hw.lcd.display(
                        f"PLAT: {plate_text[:14]}",
                        "SILAKAN TAP KARTU...",
                        "RFID CIVITAS ANDA   ",
                        f"WAKTU: {remaining} DETIK"
                    )

                # Jika mode preview kamera aktif, tetap perbarui frame agar tidak macet
                rf_frame = self.camera.read() if getattr(config, 'SHOW_PREVIEW', True) else None
                if rf_frame is not None:
                    disp = self._fit_preview(rf_frame)
                    dh, dw = disp.shape[:2]
                    cv2.rectangle(disp, (0, 0), (dw, 55), (20, 20, 20), -1)
                    cv2.putText(disp, f"PLAT TERDETEKSI: {plate_text} ({int(conf*100)}%)", (15, 25),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)
                    sub_prompt = (
                        f"AKTIVASI AUTO-BIND: TAP RFID BARU ({remaining}s) | [SPASI]: SIMULASI"
                        if needs_bind else
                        f"SILAKAN TAP RFID (Sisa: {remaining}s) | TEKAN [SPASI] UNTUK SIMULASI TAP"
                    )
                    cv2.putText(disp, sub_prompt,
                                (15, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.43, (0, 255, 255), 1)
                    cv2.imshow("Smart Parking Gate ALPR - FT Untirta", disp)
                    k = self._wait_key(60)
                    if k == ord(' ') or k == 13 or self.terminal_trigger:
                        self.terminal_trigger = False
                        matched_veh = self.db.lookup_vehicle(plate_text)
                        if matched_veh.get("matched"):
                            existing_uid = matched_veh.get("rfid_uid")
                            if existing_uid:
                                rfid_uid = existing_uid
                            else:
                                # Simulasi kartu RFID kosong baru untuk Auto-Bind
                                rfid_uid = f"0009{int(time.time()) % 1000000:06d}"
                        else:
                            # Simulasi kartu RFID baru yang di-tap oleh pengendara belum terdaftar
                            rfid_uid = f"0008{int(time.time()) % 1000000:06d}"
                        print(f"  [SIMULASI PC \u2705] Tap RFID disimulasikan via Terminal/Kamera: {rfid_uid}")
                        self.hw.buzzer_beep(0.08, 1)
                        break
                    elif k == ord('m') or k == ord('M') or self.terminal_manual:
                        self.terminal_manual = False
                        self.handle_manual_override()
                        return
                    elif k == ord('q') or k == ord('Q') or k == 27 or self.terminal_quit:
                        return

                # Cek jika ada trigger dari terminal VSCode saat preview tidak aktif
                if self.terminal_trigger:
                    self.terminal_trigger = False
                    matched_veh = self.db.lookup_vehicle(plate_text)
                    if matched_veh.get("matched") and matched_veh.get("rfid_uid"):
                        rfid_uid = matched_veh.get("rfid_uid")
                    elif matched_veh.get("matched"):
                        rfid_uid = f"0009{int(time.time()) % 1000000:06d}"
                    else:
                        rfid_uid = f"0008{int(time.time()) % 1000000:06d}"
                    print(f"  [SIMULASI PC \u2705] Tap RFID disimulasikan via Terminal VSCode: {rfid_uid}")
                    self.hw.buzzer_beep(0.08, 1)
                    break

                # Cek jika tombol manual ditekan petugas selama menunggu
                if self.hw.is_button_pressed() or self.terminal_manual:
                    self.terminal_manual = False
                    self.handle_manual_override()
                    return

                # Cek jika kartu RFID di-tap fisik
                rfid_uid = self.hw.get_scanned_rfid()
                if rfid_uid:
                    print(f"  [RFID OK] Kartu di-tap: UID {rfid_uid}")
                    self.hw.buzzer_beep(0.08, 1)
                    break

                if rf_frame is None:
                    time.sleep(0.2)

        if not rfid_uid:
            print("  [TIMEOUT] Batas waktu tap RFID habis.")
            self.hw.lcd.display(
                "WAKTU TAP HABIS!    ",
                "KARTU BELUM DI-TAP  ",
                "VERIFIKASI GAGAL    ",
                "TEKAN TOMBOL MANUAL "
            )
            self.hw.buzzer_alert()
            time.sleep(2.5)
            self.hw.lcd.display_standby()
            return

        # 3. VERIFIKASI GANDA (PLAT & RFID HARUS MATCH)
        self.hw.lcd.display(
            "MEMVERIFIKASI DATA..",
            f"PLAT: {plate_text[:14]}",
            f"RFID: {rfid_uid[:14]}",
            "MOHON TUNGGU..."
        )
        time.sleep(0.5)

        auth_result = self.db.verify_dual_auth(plate_text, rfid_uid)
        auth_result["confidence"] = conf

        if auth_result["verified"]:
            # SUKSES: Plat dan RFID cocok pada kendaraan yang sama
            self.handle_exit_success(auth_result)
        else:
            # GAGAL: Mismatch atau belum terdaftar
            self.handle_exit_failed(auth_result, reason=auth_result.get("reason", "UNREGISTERED"))

    def run(self):
        """Loop pemantauan gerbang keluar secara real-time dengan Live Camera Feed."""
        print("\n" + "=" * 65)
        print("  MEMULAI PEMANTAUAN GERBANG KELUAR (STANDBY DUAL-AUTH)")
        print("  Status Kamera: LIVE WEBCAM MENYALA TERUS MENERUS")
        print("  Pemicu Capture Plat:")
        print("    1. Sensor IR 1 (Sebelum Palang)")
        print("    2. Tekan tombol [SPASI] pada jendela kamera")
        print("  Buka Manual: Tekan [M] atau Push Button (ESP32)")
        print(f"  Status ESP32: {'TERHUBUNG (' + str(self.hw.esp.port) + ')' if self.hw.is_connected else 'BELUM TERHUBUNG (MODE SIMULASI)'}")
        print("  Keluar: Tekan [Q] atau Ctrl + C")
        print("=" * 65 + "\n")

        window_name = "Smart Parking Gate ALPR - FT Untirta"
        if getattr(config, 'SHOW_PREVIEW', True):
            try:
                cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)
            except Exception as e:
                print(f"[WARN] Tidak dapat membuka window GUI OpenCV: {e}")

        try:
            while True:
                # 1. Ambil frame terbaru dari thread kamera
                #    (thread kamera juga yang menyiarkan frame ke Web Dashboard / Live Stream Server)
                current_frame = self.camera.read()

                # 2. Tampilkan live feed pada jendela desktop jika preview aktif
                key = -1
                previewed = False
                if getattr(config, 'SHOW_PREVIEW', True) and current_frame is not None:
                    previewed = True
                    display_frame = self._fit_preview(current_frame)
                    h, w = display_frame.shape[:2]

                    # Overlay Header Bar
                    cv2.rectangle(display_frame, (0, 0), (w, 42), (25, 25, 25), -1)
                    cv2.putText(display_frame, "GERBANG KELUAR FT UNTIRTA [LIVE WEBCAM]", (15, 27),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                    time_str = time.strftime("%H:%M:%S WIB")
                    cv2.putText(display_frame, time_str, (w - 140, 27),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

                    # Panduan Area Plat di Tengah Layar (Guide Box)
                    box_w, box_h = int(w * 0.55), int(h * 0.35)
                    x1 = (w - box_w) // 2
                    y1 = (h - box_h) // 2
                    x2 = x1 + box_w
                    y2 = y1 + box_h
                    cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 255, 255), 2)
                    cv2.putText(display_frame, "ARAHKAN PLAT NOMOR KE SINI", (x1 + 10, y1 - 8),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)

                    # Overlay Footer Bar Instruksi
                    cv2.rectangle(display_frame, (0, h - 35), (w, h), (20, 20, 20), -1)
                    cv2.putText(display_frame, "TEKAN [SPASI] DI TERMINAL/KAMERA: CAPTURE | [M]: BUKA MANUAL | [Q]: KELUAR",
                                (15, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (200, 200, 200), 1)

                    cv2.imshow(window_name, display_frame)
                    key = self._wait_key(25)

                # 3. Cek Pemicu: Spasi ditekan di Terminal VSCode / Jendela Kamera ATAU Sensor IR 1
                ir_triggered = self.hw.is_vehicle_at_sensor1()
                space_triggered = (key == ord(' ') or key == 13 or self.terminal_trigger)

                if ir_triggered or space_triggered:
                    trigger_src = "SENSOR INFRARED 1" if ir_triggered else "TOMBOL [SPASI] (Terminal VSCode / Kamera)"
                    self.terminal_trigger = False
                    print(f"\n[TRIGGER] Kendaraan terdeteksi via {trigger_src}!")

                    if getattr(config, 'SHOW_PREVIEW', True) and current_frame is not None:
                        feedback = self._fit_preview(current_frame)
                        fh, fw = feedback.shape[:2]
                        cv2.rectangle(feedback, (0, 0), (fw, fh), (0, 255, 0), 4)
                        cv2.putText(feedback, "MEMOTRET & MEMPROSES PLAT NOMOR...", (25, fh // 2),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                        cv2.imshow(window_name, feedback)
                        self._show_for(0.15)

                    self.process_dual_verification(frame=current_frame)

                # 4. Cek Pemicu Tombol Keyboard [M] atau Tombol Push Button Fisik
                if (key == ord('m') or key == ord('M')) or self.terminal_manual or self.hw.is_button_pressed():
                    self.terminal_manual = False
                    self.handle_manual_override()

                # 5. Cek Tombol Keluar [Q] atau ESC
                if key == ord('q') or key == ord('Q') or key == 27 or self.terminal_quit:
                    print("\n[USER] Menutup aplikasi...")
                    break

                # 6. Pemicu Kartu: Jika pengendara men-tap RFID duluan sebelum IR
                scanned_rfid = self.hw.get_scanned_rfid()
                if scanned_rfid:
                    self.process_dual_verification(initial_rfid=scanned_rfid, frame=current_frame)

                # 7. Pemicu Remote Web: perintah buka palang dari dashboard lokal / Netlify
                #    (dipantau thread sync di background, di sini hanya mengambil antrean tanpa menunggu)
                remote_cmd = self.db.get_pending_gate_command()
                if remote_cmd:
                    self.handle_web_remote_override(remote_cmd)

                if not previewed:
                    time.sleep(0.04)

        except KeyboardInterrupt:
            print("\n[STOP] Menghentikan sistem Gerbang Keluar...")
        finally:
            self.cleanup()

    def cleanup(self):
        """Lepaskan seluruh resource hardware, kamera, dan stream server."""
        self.stop_terminal_listener = True
        self.camera.stop()
        if self.stream_server:
            self.stream_server.stop()
        if getattr(config, 'SHOW_PREVIEW', True):
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass
        self.hw.cleanup()
        self.db.close()
        print("[OK] Sistem gerbang keluar dinonaktifkan dengan aman.")


def main():
    parser = argparse.ArgumentParser(description="Smart Parking Exit Gate FT UNTIRTA (Dual-Auth)")
    parser.add_argument("--test-cam", action="store_true", help="Uji coba preview webcam USB")
    parser.add_argument("--image", type=str, help="Uji coba deteksi satu file gambar")
    args = parser.parse_args()

    gate = SmartParkingExitGate()

    if args.test_cam:
        print("[TEST] Membuka window preview webcam. Tekan 'q' untuk keluar.")
        while True:
            frame = gate.capture_frame()
            if frame is not None:
                cv2.imshow("Webcam USB Test (Exit Gate Mini PC)", self._fit_preview(frame))
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
        cv2.destroyAllWindows()
        gate.cleanup()
    elif args.image:
        print(f"[TEST] Menguji gambar: {args.image}")
        img = cv2.imread(args.image)
        if img is not None:
            res = gate.api.recognize(img)
            if res.get("results"):
                plate = res["results"][0]["plate_text"]
                print(f"Hasil: Plat={plate}")
                auth = gate.db.verify_dual_auth(plate, "0001234567")
                print(f"Dual Auth Test (dengan RFID 0001234567): Verified={auth['verified']}, Status={auth['status']}")
            else:
                print("Tidak ada plat terdeteksi.")
        gate.cleanup()
    else:
        gate.run()


if __name__ == "__main__":
    main()
