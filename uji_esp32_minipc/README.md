# Uji Komunikasi Mini PC ↔ ESP32 (LCD)

Menguji jalur **USB Serial Mini PC → ESP32 → LCD I2C** untuk LCD **16x2** dan **20x4**.
Hanya LCD: sensor IR, push button, servo, dan buzzer tidak dipakai.

| File | Fungsi |
|---|---|
| `uji_lcd.py` | Program uji di Mini PC |
| `uji_lcd_esp32/uji_lcd_esp32.ino` | Firmware uji ESP32 (ukuran LCD dipilih dari Mini PC) |

## Perlu flash firmware uji?

| Firmware di ESP32 | Mode 16x2 | Mode 20x4 |
|---|---|---|
| **Firmware uji** `uji_lcd_esp32.ino` | ✅ | ✅ |
| Firmware gerbang `smart_gate_esp32.ino` (dikompilasi 16x2) | ✅ | ⚠️ baris 3–4 tidak tampil |

Firmware uji disarankan: satu kali flash bisa menguji **kedua** ukuran (perintah `SIZE:16x2` / `SIZE:20x4`),
dan ESP32 membalas setiap perintah LCD (`OK:LCD:<ms>`) sehingga pengiriman yang hilang langsung terdeteksi.

**Flash firmware uji (Arduino IDE):** buka `uji_lcd_esp32/uji_lcd_esp32.ino`, board **ESP32 Dev Module**,
library **LiquidCrystal_I2C** (sama dengan firmware gerbang), lalu Upload. Pin: SDA GPIO21, SCL GPIO22.
Relay buzzer (GPIO26) dikunci ke posisi mati, servo tidak disentuh.

> Setelah selesai menguji, **flash kembali** `firmware/smart_gate_esp32/smart_gate_esp32.ino` agar gerbang berfungsi.

## Menjalankan

Hentikan dulu `main.py` / Live ALPR (port serial tidak boleh dipakai dua program). Dari root proyek:

```bash
venv/bin/python uji_esp32_minipc/uji_lcd.py --mode 16x2      # Mini PC (Linux)
venv/bin/python uji_esp32_minipc/uji_lcd.py --mode 20x4
python uji_esp32_minipc/uji_lcd.py --mode 20x4 --port COM5    # Laptop Windows
```

| Opsi | Keterangan |
|---|---|
| `--mode 16x2` / `20x4` | Ukuran LCD (default `LCD_COLS`x`LCD_ROWS` di `config.py`) |
| `--port` | `AUTO` (default `ESP32_PORT`), `/dev/ttyUSB0`, `COM5` |
| `--auto` | Tanpa pertanyaan y/n; uji tampilan dicatat `DILIHAT` (cek LCD sendiri) |
| `--tanpa-watchdog` | Lewati uji "MINI PC OFFLINE!" (7 detik) |

## Tahap uji

1. **Koneksi & handshake** — cari port (CP210x/CH340/FTDI/Espressif), PING→PONG, kenali firmware, alamat I2C LCD.
2. **Ukuran LCD** — firmware uji menginisialisasi ulang LCD sesuai `--mode`.
3. **PING → PONG** 20× — rata-rata / min / maks waktu respons dan jumlah yang hilang.
4. **Layar uji** (dijawab y/n setelah melihat LCD):
   tepi & urutan baris `[ BARIS n ]`, penggaris kolom `1234567890…`, semua karakter ASCII (dibandingkan dengan
   terminal), layar penuh `#`, penghitung cepat 50× (5 per detik), dan `LCDCLR`.
5. **Watchdog** — Mini PC diam 7 detik → LCD harus menampilkan `MINI PC OFFLINE!`, lalu pulih saat data dikirim lagi.

Hasil akhir: tabel **LULUS / GAGAL** di terminal dan ringkasan di LCD. Kode keluar `0` bila tidak ada yang GAGAL.

## Bila gagal

| Gejala | Periksa |
|---|---|
| Tidak ada port USB-Serial | Kabel USB data (bukan kabel daya saja), driver CP210x/CH340 |
| Port sedang dipakai program lain | Hentikan `main.py` / Live ALPR (PID ditampilkan) |
| Tidak ada balasan PONG | Firmware belum di-flash, baud bukan 115200 |
| `LCD TIDAK DITEMUKAN` | Kabel SDA 21 / SCL 22, catu 5V, GND bersama, alamat I2C |
| Teks tidak tampil tapi uji lulus | Putar potensiometer kontras di modul I2C |
| Linux: permission denied | `sudo usermod -aG dialout $USER`, lalu logout/login |
