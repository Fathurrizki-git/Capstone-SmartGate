# 🚗 Smart Parking Exit Gate FT UNTIRTA (RFID + ALPR) — Capstone A7

Sistem pemantauan dan pengendalian **gerbang KELUAR parkir pintar** berbasis verifikasi **Automatic License Plate Recognition (ALPR)** dan **RFID 13.56 MHz** yang dirancang untuk Fakultas Teknik Universitas Sultan Ageng Tirtayasa (FT UNTIRTA). 

Sistem ini difokuskan khusus pada **pintu keluar parkir (Exit Gate Only)** untuk memastikan keamanan kendaraan:
- **Verifikasi Keluar Otomatis:** Kendaraan keluar diverifikasi melalui **ALPR (Kamera Plat)** dan **Kartu RFID Civitas** sekaligus. Jika keduanya cocok pada pemilik yang sama, palang gerbang keluar otomatis terbuka.
- **Verifikasi Keluar Manual:** Untuk kendaraan tamu atau yang belum terdaftar di sistem, petugas keamanan melakukan pengecekan manual dan membuka gerbang keluar via **Push Button**.

Sistem terdiri dari **Mini PC** sebagai pusat pemrosesan (ALPR, database, web stream) dan **mikrokontroler ESP32** sebagai pengendali I/O gerbang (sensor, servo, buzzer, LCD, push button). Keduanya terhubung melalui **USB Serial**. Catu daya mandiri bertenaga surya (**PLTS + Baterai 12V**), terintegrasi secara *real-time* ke cloud database **Supabase** (PostgreSQL) dengan status arah `OUT` (Keluar).

---

## 🧭 Arsitektur Sistem

```text
                    ┌──────────────────────────── MINI PC ────────────────────────────┐
 Webcam USB  ─USB─► │ main.py: ALPR (Plate Recognizer API) • Verifikasi Plat + RFID    │ ◄─► Plate Recognizer (Internet)
 RFID USB    ─USB─► │ stream_server.py + local_api.py: Dashboard & Kamera :5050         │
                    │ gate_database.py ─► PostgreSQL LOKAL ◄─ sync_worker.py ───────────┼─► Supabase (saat online)
                    │ hardware.py ─► esp32_link.py (Serial 115200)                      │
                    └───────────────────────────────┬──────────────────────────────────┘
                                                    │ Kabel USB data
                    ┌───────────────────────────────▼──────────────────────────────────┐
                    │ ESP32 (firmware/smart_gate_esp32): debounce sensor & tombol,      │
                    │ PWM servo, pola buzzer non-blocking, LCD I2C 20x4                 │
                    └───┬────────┬────────┬────────┬────────┬────────┬─────────────────┘
                      IR 1     IR 2    Button    Servo    Buzzer   LCD 20x4
```

Pembagian tugas:
- **Mini PC** memegang seluruh logika keputusan (kapan membuka/menutup palang, verifikasi ganda, safety timer IR 2).
- **ESP32** hanya menjalankan perintah dan melaporkan status sensor. Status dikirim saat berubah (`EVT:`) dan setiap 250 ms (`ST:`).
- Jika ESP32 belum tersambung, `main.py` otomatis berjalan dalam **mode simulasi**, lalu terus mencoba menyambung ulang setiap 2 detik.
- Jika Mini PC berhenti merespons selama 5 detik, LCD menampilkan **"MINI PC OFFLINE"**. Posisi palang **tidak diubah** oleh ESP32.

### 🗄️ Database Lokal + Sinkronisasi Cloud

Mini PC memakai **PostgreSQL lokal** sebagai sumber data utama, sedangkan **Supabase** menjadi salinan cloud untuk dashboard Netlify dan registrasi mandiri `/register`.

| Data | Arah sinkronisasi | Saat internet mati |
|---|---|---|
| Log kendaraan | Lokal → Supabase | Tersimpan lokal, **diantrekan**, lalu dikirim saat online (waktu kejadian asli tetap) |
| Kendaraan terdaftar | Dua arah (tiap `SYNC_INTERVAL` detik) | Verifikasi plat + RFID tetap jalan dari data lokal |
| Akun petugas | Supabase → lokal (password di-hash PBKDF2) | Login dashboard lokal tetap bisa |
| Buka gerbang dari dashboard lokal | Langsung ke Mini PC | Tetap bisa (tanpa internet) |
| Buka gerbang dari Netlify | Supabase → Mini PC | Tidak bisa. Perintah yang lebih tua dari 60 detik diabaikan agar palang tidak terbuka tiba-tiba |
| Baca plat (ALPR) | Plate Recognizer cloud | ❌ Butuh internet: gerbang dibuka manual oleh petugas |

---

## 📌 Daftar Komponen Sistem

| No | Komponen | Terhubung ke | Fungsi & Spesifikasi |
|---|---|---|---|
| 1 | **Mini PC** | — | Pusat pemrosesan: ALPR API, database Supabase, web dashboard, logika gerbang keluar |
| 2 | **ESP32 DevKit (WROOM-32)** | USB Mini PC | Kontroler I/O gerbang (sensor, servo, buzzer, LCD, tombol) |
| 3 | **Webcam USB** | USB Mini PC | Kamera penangkap plat nomor saat kendaraan mendekati palang keluar |
| 4 | **RFID USB 13.56 MHz** | USB Mini PC | Pembaca kartu KTM / kartu civitas akademika berbasis USB HID (mode keyboard) |
| 5 | **Buzzer Flash AD16-22SM 12V** | ESP32 GPIO26 | Indikator suara & lampu strobo saat verifikasi keluar disetujui / ditolak |
| 6 | **Motor Servo (25kg/standard)** | ESP32 GPIO13 | Aktuator penggerak palang gerbang keluar (0° tertutup, 90° terbuka) |
| 7 | **LCD I2C 20x4** | ESP32 GPIO21/22 | Layar panduan instruksi pengendara dan status verifikasi keluar |
| 8 | **Push Button** | ESP32 GPIO27 | Tombol manual override petugas untuk membuka gerbang bagi tamu / belum terdaftar |
| 9 | **Sensor Infrared 1 (Sebelum Palang)** | ESP32 GPIO32 | E18-D80NK: Deteksi kedatangan kendaraan agar kamera tidak memotret terus-menerus |
| 10 | **Sensor Infrared 2 (Setelah Palang)** | ESP32 GPIO33 | E18-D80NK: Deteksi kendaraan melintas sebagai pemicu penutupan palang otomatis via safety timer |

---

## 🔌 Diagram Pinout & Wiring (ESP32 DevKit V1)

Pin dapat diubah di bagian `Pemetaan Pin` pada [smart_gate_esp32.ino](firmware/smart_gate_esp32/smart_gate_esp32.ino).

```text
ESP32 DevKit V1 (ESP32-WROOM-32):
   VIN / 5V             ------> VCC LCD I2C 20x4 & VCC Sensor IR 1 / IR 2 (E18-D80NK, 5V)
   GND                  ------> GND LCD, GND Sensor IR 1 & 2, GND Push Button, GND Buck Servo, Emitor NPN (Common GND)
   GPIO21 (SDA)         ------> SDA LCD I2C 20x4
   GPIO22 (SCL)         ------> SCL LCD I2C 20x4
   GPIO32               ------> OUT Sensor IR 1 Sebelum Palang (Active LOW, INPUT_PULLUP)
   GPIO33               ------> OUT Sensor IR 2 Setelah Palang (Active LOW, INPUT_PULLUP)
   GPIO27               ------> Push Button Manual (Active LOW, INPUT_PULLUP; kaki lain ke GND)
   GPIO13               ------> Signal Motor Servo Palang (PWM 50Hz, 500–2500 µs)
   GPIO26               ------> Resistor 1kΩ ke Basis Transistor NPN (Driver Buzzer Flash 12V)
   Port Micro-USB       ------> USB Mini PC (data + daya 5V ESP32)

Mini PC:
   Port USB 1           ------> Webcam USB
   Port USB 2           ------> RFID USB 13.56 MHz Reader
   Port USB 3           ------> ESP32 DevKit (kabel USB DATA, bukan kabel charge saja)
```

> ⚠️ **Level tegangan:** GPIO ESP32 bekerja pada **3.3V dan tidak toleran 5V**.
> - **Sensor E18-D80NK** berjenis NPN *open-collector*, sehingga aman memakai pull-up internal 3.3V ESP32. Namun jika modul Anda memiliki pull-up internal ke 5V (output terukur 5V saat bebas), pasang pembagi tegangan (misalnya 10kΩ/20kΩ) atau level shifter.
> - **Modul I2C LCD (PCF8574)** umumnya memiliki pull-up ke 5V di SDA/SCL. Disarankan memakai level shifter I2C dua arah.
> - Firmware mendeteksi alamat LCD secara otomatis (`0x27` atau `0x3F`).

### ⚡ Arsitektur Catu Daya (Solar PLTS & Buck Converter)
* **Panel Surya (PLTS)** terhubung ke **Solar Charge Controller (SCC MPPT 12V 10A)**.
* **Baterai 12V (20.000 mAh)** terhubung ke port baterai SCC MPPT.
* **Jalur Output LOAD SCC (12V):**
  - **Mini PC:** Disuplai sesuai spesifikasi adaptor Mini PC (misalnya DC-DC converter 12V ke tegangan input Mini PC). Periksa konsumsi daya Mini PC (lihat label adaptor, misalnya 12V/19V dengan daya 15–65W) dan pastikan kapasitas baterai/PLTS mencukupi untuk operasi harian.
  - **ESP32:** Mendapat daya 5V langsung dari port USB Mini PC.
  - **Buck Converter (Step-down 12V ke 5V):** Menyuplai daya arus tinggi ke Motor Servo. GND harus terhubung ke common GND ESP32.
  - **Jalur Langsung 12V:** Menyuplai daya ke Buzzer Flash AD16-22SM 12V melalui *switching* transistor NPN yang dipicu oleh GPIO26 ESP32.

---

## 🔄 Alur Logika Verifikasi Ganda & Penutup Palang Otomatis (Dual IR Sensor)

Sistem menggunakan **Dual-Factor Authentication (2FA)** dan **2 Sensor Infrared Bebas Bug**:
1. **Sensor IR 1 (Sebelum Palang):** Memicu webcam mengambil foto plat nomor dan meminta pengendara men-tap RFID. Kamera **tidak mengambil gambar terus-menerus** jika tidak ada kendaraan.
2. **Dual Verification:** Plat nomor **DAN** Kartu RFID **KEDUANYA HARUS COCOK** pada data pemilik yang sama di database `registered_vehicles`.
3. **Buka Manual (Bypass Tamu):** Jika belum terdaftar atau terjadi ketidakcocokan, petugas keamanan dapat membuka gerbang secara fisik via **Push Button (ESP32 GPIO27)** ATAU secara jarak jauh melalui **Web Dashboard (Menu Security Notification)** dengan pop-up konfirmasi keamanan.
4. **Sensor IR 2 (Setelah Palang) + Safety Timer:** Setelah palang terbuka, sistem mendeteksi mobil melintas ke Sensor 2. Ketika mobil telah sepenuhnya melewati Sensor 2, sistem mengaktifkan **Safety Timer (2.0s)** sebelum palang turun ke 0°. Jika ada kendaraan susulan memicu Sensor 2 selama timer, timer langsung **direset** agar palang tidak pernah menimpa kendaraan!

```mermaid
flowchart TD
    A["🚗 Kendaraan Mendekati Gerbang Keluar"] --> B["🔴 Sensor IR 1 Terhalang (ESP32 GPIO32)"]
    B --> C["📸 1. Webcam USB: Capture Plat Nomor (ALPR)"]
    C --> D["💳 2. Pengendara Tap Kartu RFID 13.56 MHz"]

    C & D --> E["🔍 Verifikasi Ganda ke Supabase (registered_vehicles)"]

    E -->|KEDUA DATA COCOK (Plat + RFID Sama)| F["✅ AKSES KELUAR DITERIMA"]
    F --> F1["LCD: 'HATI-HATI DI JALAN :)' & Nama Pemilik"]
    F1 --> F2["Buzzer: Beep 2x Pendek"]
    F2 --> F3["Servo: Buka Palang Keluar (90°)"]
    F3 --> F4["Kirim Log Lengkap ke Supabase (Arah: OUT)"]

    E -->|Plat & RFID BEDA / Kartu Orang Lain| G["❌ DATA TIDAK COCOK (DITOLAK)"]
    E -->|Belum Terdaftar / Tamu| H["❌ BELUM TERDAFTAR"]
    
    G & H --> I["Palang Keluar TETAP TERTUTUP + Alarm Strobo"]
    I --> J["🔘 Petugas Cek Manual: Tekan Push Button / Web Dashboard"]
    J --> F3

    F4 --> K["🚗 Kendaraan Bergerak Maju Melewati Palang"]
    K --> L["🟡 Sensor IR 2 Terdeteksi (ESP32 GPIO33 - Sedang Melintas)"]
    L --> M["🟢 Sensor IR 2 Bebas (Bodi Mobil Selesai Melintas)"]
    M --> N["⏱️ Mulai Safety Countdown Timer (2.0 Detik)"]
    N -->|Ada objek memicu IR 2 lagi| L
    N -->|Timer Selesai & Area Bebas| O["🔒 Servo: Tutup Palang Keluar (0°)"]
    O --> P["Standby Menunggu Kendaraan Berikutnya"]
```

---

## 📂 Struktur Direktori

```text
ALPR/
├── main.py                       # Program utama gerbang keluar (jalankan: python main.py)
├── config.py                     # Konfigurasi kamera, ESP32, RFID, timing, database lokal & Supabase
├── requirements.txt              # Dependensi Python untuk Mini PC / Laptop
├── netlify.toml                  # Konfigurasi deploy dashboard ke Netlify
├── cloudflared.exe               # Cloudflare Tunnel (opsional, tidak di-commit)
│
├── smart_gate/                   # KODE PROGRAM
│   ├── hardware/                 #   Perangkat keras gerbang
│   │   ├── hardware.py           #     LCD 20x4, Servo, Buzzer, IR Sensor, Button (via ESP32) & RFID USB
│   │   └── esp32_link.py         #     Komunikasi USB Serial Mini PC <-> ESP32 (protokol, auto-detect, reconnect)
│   ├── vision/                   #   Kamera & pengenalan plat
│   │   ├── camera.py             #     Webcam USB 1080p MJPG di thread terpisah + alat deteksi webcam
│   │   └── plate_recognizer.py   #     Client REST API Plate Recognizer & anotasi citra OpenCV
│   ├── database/                 #   Data & sinkronisasi
│   │   ├── gate_database.py      #     Verifikasi plat + RFID, auto-bind, log & antrean perintah buka gerbang
│   │   ├── local_db.py           #     Akses database lokal PostgreSQL (connection pool)
│   │   ├── local_schema.sql      #     Skema tabel database lokal (dibuat otomatis)
│   │   └── sync_worker.py        #     Sinkronisasi PostgreSQL lokal <-> Supabase di background
│   └── web/                      #   Server dashboard
│       ├── stream_server.py      #     Dashboard lokal + live kamera MJPEG :5050 + Cloudflare Tunnel
│       └── local_api.py          #     API dashboard lokal (/api/login, /api/logs, /api/vehicles, /api/gate/open)
│
├── scripts/                      # ALAT BANTU ADMIN (dijalankan manual)
│   ├── setup_local_db.py         #   Setup sekali: user, database, tabel & data awal dari Supabase
│   ├── manage_vehicles.py        #   CLI kelola kendaraan & kartu RFID (langsung ke Supabase)
│   └── manage_users.py           #   CLI kelola akun petugas & admin (langsung ke Supabase)
│
├── supabase/                     # SQL UNTUK SUPABASE (dijalankan di SQL Editor)
│   ├── schema.sql                #   Skema project Supabase baru
│   └── migration_sync.sql        #   Migrasi project Supabase yang sudah ada (kolom sinkronisasi)
│
├── firmware/                     # PROGRAM ESP32 (Arduino IDE)
│   └── smart_gate_esp32/
│       └── smart_gate_esp32.ino
│
├── web_dashboard/                # Web Dashboard Petugas (juga dideploy ke Netlify)
│   ├── index.html                #   Dashboard SPA (Vehicle Log, Trendline, Security, Kendaraan)
│   ├── register.html             #   Portal registrasi mandiri civitas
│   ├── supabase_config.js        #   Konfigurasi koneksi Supabase Web
│   └── img/                      #   Aset logo Untirta & Crodas
└── output/                       # Folder penyimpanan citra deteksi gerbang
```

> Semua perintah dijalankan dari **folder utama proyek** (folder yang berisi `main.py`).

---

## 🚀 Panduan Instalasi & Menjalankan (Mini PC + ESP32)

### 1. Flash Firmware ESP32 (Arduino IDE)
1. Pasang **board ESP32**: *File → Preferences → Additional Boards Manager URLs* isi
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`, lalu buka *Boards Manager* dan instal **esp32 by Espressif Systems**.
2. Pasang library melalui *Library Manager*: **ESP32Servo** dan **LiquidCrystal I2C** (Frank de Brabander).
3. Buka `firmware/smart_gate_esp32/smart_gate_esp32.ino`, pilih board **ESP32 Dev Module** dan port COM ESP32, lalu klik **Upload**.
4. Uji melalui *Serial Monitor* (115200 baud, line ending *Newline*): ketik `PING` → harus dibalas `PONG`. Status `ST:0:0:0` muncul setiap 250 ms.
5. **Tutup Serial Monitor** sebelum menjalankan `main.py`, karena port serial hanya bisa dipakai satu program.

> Jika port COM tidak muncul, instal driver USB-Serial sesuai chip board (CP210x atau CH340).

### 2. Instalasi Dependensi Python (Mini PC)
```bash
cd ALPR
pip install -r requirements.txt
```

**Khusus Mini PC Linux:** agar RFID USB dan port serial dapat dibaca tanpa `sudo`:
```bash
sudo usermod -aG dialout,input $USER   # lalu logout/login ulang
```

### 3. Konfigurasi `config.py`
Sesuaikan token Plate Recognizer, kredensial Supabase, dan port ESP32:
```python
API_TOKEN = "token_platerecognizer_anda"
SUPABASE_URL = "https://YOUR_PROJECT_ID.supabase.co"
SUPABASE_KEY = "YOUR_SUPABASE_ANON_OR_SERVICE_KEY"

ESP32_PORT = "AUTO"     # atau "COM5" (Windows) / "/dev/ttyUSB0" (Linux) jika deteksi otomatis gagal
```

**Memilih Webcam:** jalankan deteksi untuk melihat seluruh webcam yang tersambung beserta resolusi & fps nyata:
```bash
python -m smart_gate.vision.camera
```
Lalu isi `CAMERA_NAME` di `config.py` sesuai nama yang direkomendasikan (contoh: `"NYK NEMESIS"`). Kamera dipilih berdasarkan **nama**, bukan nomor index, karena index bisa bergeser saat webcam dicabut/dipasang. Kamera virtual (OBS Virtual Camera, Cast Virtual Camera) otomatis dilewati.

> Di Windows, sistem memakai backend **MSMF** (`CAMERA_BACKEND = "MSMF"`). Pada sebagian webcam, backend DirectShow mengabaikan format MJPG sehingga 1080p hanya ~1 fps.

**Pembacaan RFID USB (mode keyboard):**
- **Linux:** dibaca langsung dari perangkat input (evdev), tidak bergantung pada jendela mana yang aktif.
- **Windows:** ketikan reader ditangkap dari **jendela terminal** atau **jendela kamera OpenCV**, sehingga salah satunya harus dalam keadaan aktif (fokus).

### 4. Setup Database di Supabase (Cloud)
1. Buka Dashboard Supabase Anda, masuk ke **SQL Editor**.
2. **Project baru:** salin isi file [supabase/schema.sql](supabase/schema.sql) dan klik **Run**.
   **Project yang sudah berjalan:** jalankan [supabase/migration_sync.sql](supabase/migration_sync.sql) sekali. File ini menambah kolom `updated_at` dan `client_uuid` untuk sinkronisasi, dan data yang ada tidak terhapus.

### 5. Setup Database Lokal PostgreSQL (Mini PC)
1. Instal PostgreSQL (versi 16 ke atas) dari [postgresql.org/download](https://www.postgresql.org/download/windows/), atau lewat terminal:
   ```bash
   winget install PostgreSQL.PostgreSQL.18
   ```
   Catat **password superuser `postgres`** yang diminta saat instalasi. Service PostgreSQL otomatis menyala setiap Mini PC hidup.
2. (Opsional) Ganti password database aplikasi di `LOCAL_DB_URL` pada `config.py`.
3. Jalankan setup **sekali**. Setup membuat user, database, dan tabel, lalu menarik akun petugas & kendaraan dari Supabase:
   ```bash
   python scripts/setup_local_db.py
   ```
   Pastikan internet tersambung saat setup, agar akun petugas tersalin dan login dashboard lokal bisa dipakai saat offline nanti.

> Isi database lokal dapat dilihat dengan **pgAdmin** (ikut terpasang bersama PostgreSQL): server `localhost:5432`, database `smart_gate`.

### 6. Mengelola Data Civitas (Plat & RFID)
Cara yang disarankan adalah lewat **dashboard lokal** (menu Kendaraan / Quick-Register), karena tersimpan langsung di database lokal dan tetap bisa saat offline. Alternatif lain lewat CLI [scripts/manage_vehicles.py](scripts/manage_vehicles.py). CLI ini menulis langsung ke **Supabase** (butuh internet), lalu Mini PC menariknya dalam ±10 detik:
```bash
# Melihat daftar kendaraan & RFID terdaftar:
python scripts/manage_vehicles.py --list

# Mendaftarkan civitas baru (Plat + RFID):
python scripts/manage_vehicles.py --add --plate "A 1234 BC" --name "Ahmad Zaki" --category "STUDENT" --nim "3332210001" --rfid "0001234567"

# Melihat log deteksi gerbang terbaru:
python scripts/manage_vehicles.py --logs
```

### 7. Menjalankan Sistem Gerbang
Jalankan program utama:
```bash
python main.py
```

Pada saat start, terminal menampilkan `[OK] ESP32 terhubung pada port ...`. Jika ESP32 belum tersambung, sistem berjalan dalam **mode simulasi**: sensor/servo/buzzer/LCD dicetak di console, dan koneksi dicoba ulang otomatis.

*Untuk uji coba preview Webcam USB:*
```bash
python main.py --test-cam
```

---

## 💻 Menjalankan Web Dashboard Petugas

1. Buka folder `web_dashboard/`.
2. Buka `index.html` langsung di browser, atau jalankan web server:
   ```bash
   cd web_dashboard
   python -m http.server 8080
   ```
3. Akses via browser: `http://<IP-Mini-PC>:8080` (atau `http://<IP-Mini-PC>:5050` saat `main.py` berjalan, lengkap dengan live kamera)

### Lokal vs Cloudflare Tunnel

| Mode | Pengaturan `config.py` | Akses Dashboard | Cocok untuk |
|---|---|---|---|
| **Lokal (default, disarankan)** | `CLOUDFLARE_TUNNEL_ENABLED = False` | `http://<IP-Mini-PC>:5050` dari perangkat di jaringan WiFi/LAN yang sama | Pos satpam di dekat gerbang; latensi kamera paling rendah, tidak terbuka ke internet |
| **Cloudflare Tunnel** | `CLOUDFLARE_TUNNEL_ENABLED = True` + `cloudflared` | URL `https://xxxx.trycloudflare.com` (berganti setiap restart), diisi di tombol *URL Cloudflare* pada dashboard Netlify | Memantau kamera dari luar jaringan kampus |

Pada mode lokal, dashboard memutar kamera lewat **MJPEG push** (`/video_feed`): server mengirim setiap frame begitu tersedia melalui satu koneksi terbuka. Mode Cloudflare memakai polling `/snapshot` (1 request per frame) karena Cloudflare mem-buffer MJPEG. Kualitas stream lokal diatur di `config.py`:

| Parameter | Default | Efek |
|---|---|---|
| `STREAM_WIDTH` | `960` | Lebar frame stream. Turunkan (misal `640`) jika WiFi pos satpam lemah |
| `STREAM_JPEG_QUALITY` | `70` | Kualitas JPEG (50–80). Lebih rendah = frame lebih kecil & cepat |
| `STREAM_MAX_FPS` | `30` | Batas fps stream (`0` = ikuti fps kamera) |

Database (log, login, kendaraan) selalu tersimpan di Supabase, sehingga **kedua mode tetap membutuhkan internet**. Halaman registrasi mandiri `/register` versi Netlify tetap bisa dipakai civitas dari mana saja tanpa tunnel, karena halaman itu tidak memakai kamera.
   - **Login Demo:** `admin@untirta.ac.id` | Password: `admin123`

---

## 👥 Pengembang
* **Tim Capstone A7** — Fakultas Teknik, Universitas Sultan Ageng Tirtayasa (UNTIRTA).
