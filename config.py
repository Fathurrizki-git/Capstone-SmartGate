"""
Konfigurasi Sistem Smart Parking Gate FT UNTIRTA
Arsitektur: Mini PC (Pemrosesan Utama) + ESP32 (Kontrol I/O Gerbang) via USB Serial
Komponen:
  Terhubung ke Mini PC (USB):
    1. Mini PC (ALPR, Database Supabase, Web Stream, Logika Gerbang)
    2. Webcam USB
    3. RFID USB 13.56 MHz
    4. ESP32 DevKit (Kabel USB data — Serial 115200)
  Terhubung ke ESP32 (pin GPIO ESP32, lihat firmware/smart_gate_esp32/smart_gate_esp32.ino):
    5. Sensor IR 1 E18-D80NK Sebelum Palang (GPIO32)
    6. Sensor IR 2 E18-D80NK Setelah Palang (GPIO33)
    7. Push Button Manual (GPIO27)
    8. Motor Servo Palang (GPIO13)
    9. Buzzer Flash AD16-22SM 12V via Transistor NPN (GPIO26)
   10. LCD I2C 20x4 (SDA: GPIO21, SCL: GPIO22)
"""

# ── Plate Recognizer API ────────────────────────
API_TOKEN = "476ed9b4276c169486fc16e30891cc01f9f0e4f1"          # Token dari platerecognizer.com
API_URL = "https://api.platerecognizer.com/v1/plate-reader/"
API_REGIONS = ["id"]                       # Region Indonesia
API_TIMEOUT = 8                            # Timeout request (detik)

# Cek webcam yang tersambung beserta nama, resolusi & fps: python -m smart_gate.vision.camera
CAMERA_NAME = "NYK NEMESIS"                 # Nama webcam yang dipakai (sebagian nama cukup). "" = webcam fisik pertama
CAMERA_INDEX = 2                            # Cadangan jika paket cv2-enumerate-cameras tidak terpasang
CAMERA_BACKEND = "MSMF"                     # Windows: "MSMF" (disarankan, 1080p MJPG 30fps) atau "DSHOW". Linux otomatis V4L2
CAMERA_WIDTH = 1920                         # Resolusi capture webcam (1080p). Jika kamera tidak mendukung,
CAMERA_HEIGHT = 1080                        # otomatis memakai resolusi maksimal kamera
CAMERA_FPS = 30
CAMERA_MJPG = True                          # Format MJPG wajib untuk 1080p @30fps (YUY2 hanya ~5 fps)
PREVIEW_WIDTH = 960                         # Lebar jendela preview desktop (frame diperkecil agar muat di layar)
MAX_IMAGE_SIZE = (1920, 1080)               # Batas maksimal dimensi citra sebelum dikirim ke API
JPEG_QUALITY = 85                           # Kualitas kompresi upload citra

# ── Mikrokontroler ESP32 (Sensor IR, Servo, Buzzer, LCD, Push Button) ─
# Pemetaan pin GPIO kini berada di firmware ESP32 (firmware/smart_gate_esp32/).
ESP32_PORT = "AUTO"          # "AUTO" = deteksi otomatis, atau isi manual: "COM5" (Windows) / "/dev/ttyUSB0" (Linux)
ESP32_BAUD = 115200          # Harus sama dengan SERIAL_BAUD di firmware ESP32
ESP32_HEARTBEAT_TIMEOUT = 2.0  # ESP32 dianggap terputus jika tidak ada data selama N detik

# ── RFID USB 13.56 MHz (Mode Keyboard / HID) ─────
RFID_MIN_LENGTH = 4          # Panjang minimal UID agar ketikan dianggap kartu RFID
RFID_DEVICE_KEYWORDS = ["rfid", "reader", "card", "hid"]  # Kata kunci nama perangkat (Linux / evdev)

# ── LCD I2C 20x4 (Terpasang di ESP32) ────────────
LCD_COLS = 20
LCD_ROWS = 4

# ── Parameter Palang Gerbang Keluar & Timing ─────
GATE_DIRECTION = "OUT"       # Default arah log kendaraan: OUT (Keluar)
SERVO_OPEN_ANGLE = 90        # Sudut palang terbuka (derajat)
SERVO_CLOSE_ANGLE = 0        # Sudut palang tertutup (derajat)
RFID_WAIT_TIMEOUT = 12.0     # Batas waktu tunggu tap RFID setelah plat terbaca (detik)
GATE_CLOSE_DELAY = 2.0       # Timer jeda setelah Sensor IR 2 bebas sebelum palang menutup (detik)
GATE_MAX_WAIT = 25.0         # Batas waktu maksimal tunggu kendaraan melintas sebelum fail-safe (detik)

# ── Database Lokal (PostgreSQL di Mini PC) ─────────
# Sumber data utama gerbang: tetap berfungsi tanpa internet. Buat database & user-nya: python scripts/setup_local_db.py
LOCAL_DB_URL = "postgresql://smart_gate:smartgate123@localhost:5432/smart_gate"
SYNC_INTERVAL = 10           # Interval sinkronisasi database lokal <-> Supabase (detik)
GATE_COMMAND_MAX_AGE = 60    # Perintah buka gerbang dari Netlify yang lebih tua dari ini diabaikan (detik)

# ── Database Cloud (Supabase) — Salinan untuk Netlify & Registrasi Mandiri ──
# Dapatkan dari: Supabase Dashboard -> Project Settings -> API
SUPABASE_URL = "https://endnfikkzdixablcpamy.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVuZG5maWtremRpeGFibGNwYW15Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTE5ODMzNzMsImV4cCI6MjEwNzU1OTM3M30.4AcnOGYLdHVIPDsQlt2bILxOJ2nswr8OPpgfnDCFqN0"

# ── Penyimpanan & Debug ──────────────────────────
SAVE_RESULTS = True                         # Simpan foto hasil deteksi ke folder output/
OUTPUT_DIR = "output/"                      # Folder penyimpanan citra
SHOW_PREVIEW = True                         # Set True untuk menampilkan jendela live kamera desktop
STREAM_ENABLED = True                       # Siarkan live video ke Web Dashboard via HTTP MJPEG
STREAM_PORT = 5050                          # Port dashboard lokal + kamera (http://<IP-Mini-PC>:5050)

# ── Live Stream Dashboard Lokal (Latensi Rendah) ──
# Di jaringan lokal dashboard memakai MJPEG push (/video_feed): setiap frame langsung dikirim saat tersedia.
STREAM_WIDTH = 960                          # Lebar frame stream (1080p diperkecil). Lebih kecil = lebih ringan & cepat
STREAM_JPEG_QUALITY = 70                    # Kualitas JPEG stream (50-80). Lebih rendah = ukuran kecil, latensi turun
STREAM_MAX_FPS = 30                         # Batas fps stream (0 = ikuti fps kamera)
CLOUDFLARE_TUNNEL_ENABLED = False           # False = dashboard lokal saja (disarankan, latensi terendah)
                                            # True  = buat URL HTTPS publik (dashboard Netlify / akses luar jaringan)
