"""
Modul Kontrol Perangkat Keras (Hardware Controller)
Arsitektur: Mini PC + Mikrokontroler ESP32 (USB Serial)

Mendukung Komponen:
Terhubung langsung ke Mini PC:
1. Webcam USB (via OpenCV, dikelola di main.py)
2. RFID USB 13.56 MHz (mode keyboard HID)
   - Linux  : dibaca langsung dari perangkat input via evdev (background listener)
   - Windows: ketikan reader ditangkap dari terminal / jendela kamera via feed_keyboard_char()
Terhubung ke ESP32 (dikendalikan via esp32_link.py):
3. Dual Sensor Infrared E18-D80NK:
   - Sensor IR 1 (Sebelum Palang): Deteksi Kedatangan / ALPR
   - Sensor IR 2 (Setelah Palang): Pemicu Penutup Palang
4. Push Button Manual
5. Motor Servo Palang
6. Buzzer Flash AD16-22SM 12V
7. LCD I2C 20x4

Didesain fleksibel:
- Berjalan otomatis dengan perangkat keras nyata saat ESP32 terhubung.
- Berjalan aman dalam mode Simulasi jika ESP32 belum terhubung.
"""

import time
import string
import threading
import queue
import config
from smart_gate.hardware.esp32_link import ESP32Link


def _lcd_safe(text: str) -> str:
    """LCD HD44780 hanya mendukung ASCII; '|' dipakai sebagai pemisah baris protokol."""
    return "".join(c if 32 <= ord(c) < 127 and c != '|' else ' ' for c in str(text))


class LCDController:
    """Pengontrol LCD I2C 20x4 yang terpasang pada ESP32."""
    def __init__(self, link: ESP32Link, cols=config.LCD_COLS, rows=config.LCD_ROWS):
        self.link = link
        self.cols = cols
        self.rows = rows
        self.lines = [""] * rows
        self._sent_lines = None

    def _push(self, force=False) -> bool:
        """Kirim isi LCD ke ESP32 (hanya jika berubah, agar tidak berkedip)."""
        if not self.link.connected:
            return False
        if not force and self._sent_lines == self.lines:
            return True
        payload = "|".join(_lcd_safe(l)[:self.cols].ljust(self.cols) for l in self.lines)
        if self.link.send("LCD:" + payload):
            self._sent_lines = list(self.lines)
            return True
        return False

    def redraw(self):
        """Tulis ulang seluruh isi LCD (dipanggil saat ESP32 reset / tersambung ulang)."""
        self._push(force=True)

    def clear(self):
        self.lines = [""] * self.rows
        if self.link.send("LCDCLR"):
            self._sent_lines = list(self.lines)

    def display(self, line1="", line2="", line3="", line4=""):
        """Tampilkan 4 baris teks pada LCD 20x4 (otomatis center/rata)."""
        self.lines = [line1[:self.cols], line2[:self.cols], line3[:self.cols], line4[:self.cols]]

        # Cetak ke layar fisik via ESP32 jika terhubung
        if self._push():
            return

        # Tampilan Simulasi di Console
        print("\n+--------------------+ (LCD 20x4)")
        for l in self.lines:
            print(f"|{l.center(self.cols)}|")
        print("+--------------------+\n")

    def display_standby(self):
        """Tampilan standby gerbang keluar."""
        self.display(
            " GERBANG KELUAR FT  ",
            "  UNTIRTA CILEGON   ",
            "SCAN PLAT / TAP RFID",
            "MOHON DEKATI PALANG "
        )


class HardwareManager:
    """
    Manajer terpusat untuk seluruh perangkat keras Smart Parking Gate FT Untirta.
    """
    def __init__(self):
        self.esp = ESP32Link()
        self.lcd = LCDController(self.esp)
        self.esp.on_ready = self.lcd.redraw
        self.rfid_queue = queue.Queue()
        self._rfid_buffer = ""
        self._rfid_last_key = 0.0

        if self.esp.connected:
            print("[OK] ESP32 siap mengendalikan Sensor IR, Push Button, Servo, Buzzer, dan LCD.")
        else:
            print("[SIMULASI] ESP32 belum terhubung (Sensor, Button, Buzzer, Servo, dan LCD disimulasikan).")

        # Inisialisasi thread pembaca RFID USB
        self._start_rfid_listener()

    @property
    def is_connected(self) -> bool:
        """True jika ESP32 terhubung (mode perangkat keras nyata)."""
        return self.esp.connected

    def set_servo_angle(self, angle: float):
        """Atur sudut servo palang gerbang."""
        if self.esp.connected and self.esp.send(f"SERVO:{int(angle)}"):
            time.sleep(0.35)  # Beri waktu servo mencapai posisi
        else:
            state = "TERBUKA (90°)" if angle >= 45 else "TERTUTUP (0°)"
            print(f"  [SERVO] Palang diputar ke sudut {angle}° -> Posisi: {state}")

    def open_gate(self):
        """Buka palang gerbang."""
        self.set_servo_angle(config.SERVO_OPEN_ANGLE)
        print("  [GERBANG] Palang TERBUKA.")

    def close_gate(self):
        """Tutup palang gerbang."""
        self.set_servo_angle(config.SERVO_CLOSE_ANGLE)
        print("  [GERBANG] Palang TERTUTUP.")

    def buzzer_beep(self, duration=0.15, count=1):
        """Bunyikan buzzer (konfirmasi akses diterima). Pola bunyi dijalankan non-blocking di ESP32."""
        if not (self.esp.connected and self.esp.send(f"BEEP:{int(duration * 1000)}:{count}")):
            print(f"  [BUZZER] BEEP! x{count} (Flash)")

    def buzzer_alert(self):
        """Bunyikan alarm buzzer panjang (akses ditolak / tidak terdaftar)."""
        if not (self.esp.connected and self.esp.send("ALERT:800")):
            print("  [BUZZER] ALARM PANJANG! (Akses Ditolak)")

    def is_vehicle_at_sensor1(self) -> bool:
        """Cek apakah Sensor IR 1 (sebelum palang) mendeteksi kendaraan mendekat."""
        return self.esp.connected and self.esp.ir_before

    def is_vehicle_at_sensor2(self) -> bool:
        """Cek apakah Sensor IR 2 (setelah palang) mendeteksi kendaraan melintas."""
        return self.esp.connected and self.esp.ir_after

    def is_button_pressed(self) -> bool:
        """
        Cek apakah tombol push button ditekan oleh petugas.
        Setiap tekanan (sudah di-debounce oleh ESP32) hanya dilaporkan sekali.
        """
        return self.esp.pop_button_press()

    def feed_keyboard_char(self, ch: str) -> bool:
        """
        Tampung ketikan dari RFID USB mode keyboard (Windows: terminal / jendela kamera).
        Reader mengetik UID dengan sangat cepat lalu diakhiri ENTER.
        Return True jika karakter dipakai sebagai bagian UID (tidak perlu diproses sebagai tombol).
        """
        now = time.time()
        if now - self._rfid_last_key > 0.5:
            self._rfid_buffer = ""
        self._rfid_last_key = now

        if ch in ('\r', '\n'):
            uid = self._rfid_buffer
            self._rfid_buffer = ""
            if len(uid) >= getattr(config, 'RFID_MIN_LENGTH', 4):
                self.rfid_queue.put(uid)
                return True
            return False

        if ch in string.hexdigits:
            self._rfid_buffer += ch.upper()
            return True
        return False

    def _start_rfid_listener(self):
        """
        Listener background untuk membaca kartu RFID USB (13.56 MHz) di Linux.
        Umumnya USB RFID reader bertindak sebagai keyboard HID.
        """
        def _listen():
            # Jika dijalankan pada Mini PC Linux dengan evdev terpasang
            try:
                import evdev
                keywords = getattr(config, 'RFID_DEVICE_KEYWORDS', ['rfid', 'reader', 'card', 'hid'])
                devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
                rfid_dev = None
                for dev in devices:
                    # Cari perangkat RFID / barcode USB reader
                    if any(term in dev.name.lower() for term in keywords):
                        rfid_dev = dev
                        break

                if rfid_dev:
                    print(f"[OK] RFID USB terdeteksi: {rfid_dev.name}")
                    try:
                        rfid_dev.grab()  # Ketikan reader tidak ikut masuk ke terminal / jendela lain
                    except Exception:
                        pass
                    card_buffer = ""
                    for event in rfid_dev.read_loop():
                        if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                            key_char = evdev.ecodes.KEY[event.code]
                            if 'KEY_ENTER' in key_char:
                                if card_buffer.strip():
                                    self.rfid_queue.put(card_buffer.strip())
                                card_buffer = ""
                            elif 'KEY_' in key_char:
                                char = key_char.replace('KEY_', '')
                                if len(char) == 1:
                                    card_buffer += char
            except Exception:
                # Mode fallback / non-Linux (Windows memakai feed_keyboard_char)
                pass

        threading.Thread(target=_listen, daemon=True).start()

    def get_scanned_rfid(self):
        """Ambil UID kartu RFID jika ada yang di-tap."""
        try:
            return self.rfid_queue.get_nowait()
        except queue.Empty:
            return None

    def cleanup(self):
        """Lepaskan koneksi ESP32 saat aplikasi ditutup."""
        print("[INFO] Membersihkan resource perangkat keras...")
        try:
            self.lcd.clear()
            self.esp.close()
        except Exception:
            pass
