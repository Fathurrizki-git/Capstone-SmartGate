"""
Modul Kontrol Perangkat Keras (Hardware Controller)
Perangkat: Raspberry Pi 3 Model B+

Mendukung Komponen:
1. Raspberry Pi 3 Model B+ (Sistem Utama)
2. Webcam USB (via OpenCV)
3. RFID USB 13.56 MHz (Background USB Listener)
4. Buzzer Flash AD16-22SM 12V (GPIO22 - Pin 15)
5. Motor Servo Palang (GPIO18 - Pin 12)
6. LCD I2C 20x4 (SDA: GPIO2 / Pin 3, SCL: GPIO3 / Pin 5)
7. Push Button Manual (GPIO27 - Pin 13)
8. Dual Sensor Infrared E18-D80NK:
   - Sensor IR 1 (Sebelum Palang): GPIO 17 - Pin 11 (Deteksi Kedatangan / ALPR)
   - Sensor IR 2 (Setelah Palang): GPIO 23 - Pin 16 (Pemicu Penutup Palang)

Didesain fleksibel:
- Berjalan otomatis di Raspberry Pi dengan GPIO nyata.
- Berjalan aman di PC / Windows dalam mode Simulasi (tanpa error RPi.GPIO).
"""

import time
import threading
import queue
import config

# Cek ketersediaan RPi.GPIO (Deteksi apakah berjalan di Raspberry Pi)
HAS_RPI_GPIO = False
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    HAS_RPI_GPIO = True
except (ImportError, RuntimeError):
    HAS_RPI_GPIO = False

# Cek ketersediaan modul LCD RPLCD
HAS_RPLCD = False
try:
    from RPLCD.i2c import CharLCD
    HAS_RPLCD = True
except ImportError:
    HAS_RPLCD = False


class LCDController:
    """Pengontrol LCD I2C 20x4."""
    def __init__(self, i2c_addr=config.LCD_I2C_ADDR, cols=config.LCD_COLS, rows=config.LCD_ROWS):
        self.cols = cols
        self.rows = rows
        self.lcd = None
        self.enabled = False

        if HAS_RPLCD:
            try:
                self.lcd = CharLCD(
                    i2c_expander='PCF8574',
                    address=i2c_addr,
                    port=config.LCD_I2C_BUS,
                    cols=cols,
                    rows=rows,
                    charmap='A00'
                )
                self.lcd.clear()
                self.enabled = True
                print("[OK] LCD I2C 20x4 terhubung.")
            except Exception as e:
                print(f"[WARN] LCD I2C belum terhubung di alamat {hex(i2c_addr)}: {e}")
        else:
            print("[INFO] Mode Simulasi LCD 20x4 aktif.")

    def clear(self):
        if self.enabled and self.lcd:
            try:
                self.lcd.clear()
            except Exception:
                pass

    def display(self, line1="", line2="", line3="", line4=""):
        """Tampilkan 4 baris teks pada LCD 20x4 (otomatis center/rata)."""
        lines = [line1[:self.cols], line2[:self.cols], line3[:self.cols], line4[:self.cols]]
        
        # Cetak ke layar fisik jika terpasang
        if self.enabled and self.lcd:
            try:
                self.lcd.clear()
                for idx, text in enumerate(lines):
                    self.lcd.cursor_pos = (idx, 0)
                    self.lcd.write_string(text.ljust(self.cols))
                return
            except Exception as e:
                print(f"[WARN] Gagal update LCD fisik: {e}")

        # Tampilan Simulasi di Console
        print("\n+--------------------+ (LCD 20x4)")
        for l in lines:
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
        self.is_rpi = HAS_RPI_GPIO
        self.lcd = LCDController()
        self.servo_pwm = None
        self.gate_is_open = False
        self.rfid_queue = queue.Queue()

        if self.is_rpi:
            print("[OK] Menginisialisasi GPIO Raspberry Pi 3 Model B+...")
            # 1. Dual Sensor Infrared E18-D80NK (Active LOW: 0 = Terdeteksi, 1 = Bebas)
            # Sensor 1 (Sebelum Palang): Pemicu deteksi kedatangan & ALPR
            GPIO.setup(config.PIN_IR_BEFORE, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            # Sensor 2 (Setelah Palang): Pemicu penutupan palang otomatis
            GPIO.setup(config.PIN_IR_AFTER, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            
            # 2. Push Button Manual (Active LOW)
            GPIO.setup(config.PIN_PUSH_BUTTON, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            
            # 3. Buzzer Flash 12V (Active HIGH)
            GPIO.setup(config.PIN_BUZZER, GPIO.OUT)
            GPIO.output(config.PIN_BUZZER, GPIO.LOW)
            
            # 4. Motor Servo (PWM 50Hz pada GPIO 18)
            GPIO.setup(config.PIN_SERVO, GPIO.OUT)
            self.servo_pwm = GPIO.PWM(config.PIN_SERVO, 50) # 50Hz frekuensi standar servo
            self.servo_pwm.start(self._angle_to_duty(config.SERVO_CLOSE_ANGLE))
            time.sleep(0.3)
            self.servo_pwm.ChangeDutyCycle(0) # Lepas sinyal agar tidak jitter
        else:
            print("[SIMULASI] Mode PC aktif (Sensor, Button, Buzzer, dan Servo disimulasikan).")

        # Inisialisasi thread pembaca RFID USB
        self._start_rfid_listener()

    def _angle_to_duty(self, angle: float) -> float:
        """Konversi sudut 0-180 derajat ke duty cycle PWM (2.5% - 12.5%)."""
        return 2.5 + (angle / 180.0) * 10.0

    def set_servo_angle(self, angle: float):
        """Atur sudut servo palang gerbang."""
        if self.is_rpi and self.servo_pwm:
            duty = self._angle_to_duty(angle)
            self.servo_pwm.ChangeDutyCycle(duty)
            time.sleep(0.35)
            self.servo_pwm.ChangeDutyCycle(0) # Mencegah dengung / panas servo
        else:
            state = "TERBUKA (90°)" if angle >= 45 else "TERTUTUP (0°)"
            print(f"  [SERVO] Palang diputar ke sudut {angle}° -> Posisi: {state}")

    def open_gate(self):
        """Buka palang gerbang."""
        self.gate_is_open = True
        self.set_servo_angle(config.SERVO_OPEN_ANGLE)
        print("  [GERBANG] Palang TERBUKA.")

    def close_gate(self):
        """Tutup palang gerbang."""
        self.gate_is_open = False
        self.set_servo_angle(config.SERVO_CLOSE_ANGLE)
        print("  [GERBANG] Palang TERTUTUP.")

    def buzzer_beep(self, duration=0.15, count=1):
        """Bunyikan buzzer (konfirmasi akses diterima)."""
        def _run():
            for _ in range(count):
                if self.is_rpi:
                    GPIO.output(config.PIN_BUZZER, GPIO.HIGH)
                    time.sleep(duration)
                    GPIO.output(config.PIN_BUZZER, GPIO.LOW)
                    time.sleep(0.08)
                else:
                    print("  [BUZZER] BEEP! (Flash)")
                    time.sleep(duration)
        threading.Thread(target=_run, daemon=True).start()

    def buzzer_alert(self):
        """Bunyikan alarm buzzer panjang (akses ditolak / tidak terdaftar)."""
        def _run():
            if self.is_rpi:
                GPIO.output(config.PIN_BUZZER, GPIO.HIGH)
                time.sleep(0.8)
                GPIO.output(config.PIN_BUZZER, GPIO.LOW)
            else:
                print("  [BUZZER] ALARM PANJANG! (Akses Ditolak)")
        threading.Thread(target=_run, daemon=True).start()

    def is_vehicle_at_sensor1(self) -> bool:
        """
        Cek apakah Sensor IR 1 (sebelum palang) mendeteksi kendaraan mendekat.
        Sensor E18-D80NK aktif LOW (0 = ada kendaraan, 1 = tidak ada).
        """
        if self.is_rpi:
            return GPIO.input(config.PIN_IR_BEFORE) == GPIO.LOW
        return False

    def is_vehicle_at_sensor2(self) -> bool:
        """
        Cek apakah Sensor IR 2 (setelah palang) mendeteksi kendaraan melintas.
        Sensor E18-D80NK aktif LOW (0 = ada kendaraan, 1 = tidak ada).
        """
        if self.is_rpi:
            return GPIO.input(config.PIN_IR_AFTER) == GPIO.LOW
        return False

    def is_vehicle_detected(self) -> bool:
        """Alias untuk is_vehicle_at_sensor1() untuk kompatibilitas."""
        return self.is_vehicle_at_sensor1()

    def is_button_pressed(self) -> bool:
        """
        Cek apakah tombol push button ditekan oleh petugas.
        Push Button aktif LOW (0 = ditekan, 1 = dilepas).
        """
        if self.is_rpi:
            return GPIO.input(config.PIN_PUSH_BUTTON) == GPIO.LOW
        return False

    def wait_for_vehicle_clear(self, timeout=8.0):
        """
        Tunggu sampai kendaraan melewati gerbang (sensor IR kembali HIGH / tidak ada objek)
        sebelum palang otomatis ditutup demi keselamatan.
        """
        start = time.time()
        print("  [SAFETY] Menunggu kendaraan selesai melintas...")
        time.sleep(1.5) # Jeda awal saat kendaraan mulai maju
        
        while time.time() - start < timeout:
            if not self.is_vehicle_detected():
                time.sleep(1.0) # Pastikan benar-benar lewat
                return True
            time.sleep(0.2)
        return False

    def _start_rfid_listener(self):
        """
        Listener background untuk membaca kartu RFID USB (13.56 MHz).
        Umumnya USB RFID reader bertindak sebagai keyboard HID.
        """
        def _listen():
            # Jika dijalankan pada RPi dengan evdev terpasang
            try:
                import evdev
                devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
                rfid_dev = None
                for dev in devices:
                    # Cari perangkat RFID / barcode USB reader
                    if any(term in dev.name.lower() for term in ['rfid', 'reader', 'card', 'hid']):
                        rfid_dev = dev
                        break

                if rfid_dev:
                    print(f"[OK] RFID USB terdeteksi: {rfid_dev.name}")
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
                # Mode fallback / non-Linux
                pass

        threading.Thread(target=_listen, daemon=True).start()

    def get_scanned_rfid(self):
        """Ambil UID kartu RFID jika ada yang di-tap."""
        try:
            return self.rfid_queue.get_nowait()
        except queue.Empty:
            return None

    def cleanup(self):
        """Lepaskan seluruh resource GPIO saat aplikasi ditutup."""
        print("[INFO] Membersihkan resource perangkat keras...")
        try:
            self.lcd.clear()
            if self.is_rpi and self.servo_pwm:
                self.servo_pwm.stop()
            if self.is_rpi:
                GPIO.cleanup()
        except Exception:
            pass

