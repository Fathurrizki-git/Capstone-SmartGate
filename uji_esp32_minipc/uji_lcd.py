"""
Uji komunikasi Mini PC <-> ESP32 lewat LCD I2C (USB Serial 115200). Hanya LCD:
tidak membaca sensor IR / push button dan tidak menggerakkan servo / buzzer.

    python uji_esp32_minipc/uji_lcd.py --mode 16x2
    python uji_esp32_minipc/uji_lcd.py --mode 20x4
    python uji_esp32_minipc/uji_lcd.py --mode 20x4 --port /dev/ttyUSB0 --auto --tanpa-watchdog

Firmware ESP32:
  - uji_esp32_minipc/uji_lcd_esp32/uji_lcd_esp32.ino (disarankan): mendukung 16x2 DAN 20x4 (dipilih
    lewat perintah SIZE:), membalas setiap perintah LCD (OK:LCD:<ms>) sehingga pengiriman bisa diverifikasi.
  - firmware gerbang smart_gate_esp32.ino: hanya ukuran yang dikompilasi (saat ini 16x2), tanpa balasan LCD.

Tahap uji: 1 koneksi & handshake, 2 ukuran LCD, 3 waktu PING->PONG, 4 layar uji (tepi & urutan baris,
penggaris kolom, semua karakter, layar penuh, penghitung cepat, LCDCLR), 5 watchdog "MINI PC OFFLINE!".
Hentikan main.py / Live ALPR dulu: port serial tidak boleh dipakai dua program.
"""

import argparse
import os
import queue
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("[ERROR] Modul pyserial belum terpasang: pip install pyserial")
    sys.exit(1)

from smart_gate.hardware.esp32_link import KNOWN_USB_VIDS  # noqa: E402

UJI_FW = "UJI_LCD_ESP32"
PING_COUNT = 20
COUNTER_STEPS, COUNTER_HZ = 50, 5
WATCHDOG_WAIT = 7          # detik; firmware menampilkan peringatan setelah 5 detik tanpa data


# ── Koneksi serial ───────────────────────────────────────────────────────
class Link:
    """Port serial + thread pembaca (baris masuk ke antrean) + keepalive PING tiap 1 detik."""

    def __init__(self, ser):
        self.ser = ser
        self.q = queue.Queue()
        self.keepalive = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._keeper, daemon=True).start()

    def _reader(self):
        while not self.stop.is_set():
            try:
                raw = self.ser.readline()
            except (serial.SerialException, OSError, TypeError) as e:
                if not self.stop.is_set():
                    self.q.put((time.time(), f"__PUTUS__:{e}"))
                return
            line = raw.decode('ascii', errors='ignore').strip() if raw else ''
            if line:
                self.q.put((time.time(), line))

    def _keeper(self):
        while not self.stop.is_set():
            if self.keepalive.is_set():
                try:
                    self.send("PING")
                except ConnectionError:
                    return
            time.sleep(1.0)

    def send(self, cmd):
        try:
            with self.lock:
                self.ser.write((cmd + "\n").encode('ascii', errors='replace'))
                self.ser.flush()
            return time.time()
        except (serial.SerialException, OSError) as e:
            raise ConnectionError(f"gagal mengirim ke ESP32: {e}")

    def wait(self, pred, timeout):
        """Tunggu baris yang memenuhi pred. Baris lain (PONG keepalive, ST:, EVT:) diabaikan.
        Mengembalikan (waktu, baris) atau (None, None) bila waktu habis."""
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None, None
            try:
                t, line = self.q.get(timeout=remaining)
            except queue.Empty:
                return None, None
            if line.startswith("__PUTUS__"):
                raise ConnectionError(line.split(":", 1)[1])
            if pred(line):
                return t, line
            if line.startswith("ERR:") and line != "ERR:UNKNOWN:INFO":
                print(f"   [WARN] ESP32 melaporkan: {line}")

    def collect(self, seconds):
        out, deadline = [], time.time() + seconds
        while True:
            t, line = self.wait(lambda l: True, max(0.0, deadline - time.time()))
            if line is None:
                return out
            out.append(line)

    def drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    def close(self):
        self.stop.set()
        try:
            self.ser.close()
        except Exception:
            pass


def port_users(dev):
    """Linux: proses lain yang sedang membuka port serial yang sama (mis. main.py)."""
    if os.name != 'posix' or not os.path.isdir('/proc'):
        return []
    target, me, users = os.path.realpath(dev), os.getpid(), []
    for pid in filter(str.isdigit, os.listdir('/proc')):
        if int(pid) == me:
            continue
        try:
            for fd in os.listdir(f'/proc/{pid}/fd'):
                if os.path.realpath(f'/proc/{pid}/fd/{fd}') == target:
                    with open(f'/proc/{pid}/cmdline', 'rb') as f:
                        users.append(f"PID {pid}: {f.read().replace(b'\0', b' ').decode(errors='ignore').strip()}")
                    break
        except OSError:
            continue
    return users


def connect(port_arg, baud):
    """Buka port ESP32, tunggu boot, handshake PING/PONG. Mengembalikan (link, info) atau (None, pesan)."""
    if port_arg and str(port_arg).upper() != 'AUTO':
        ports = [port_arg]
    else:
        ports = [p.device for p in list_ports.comports() if p.vid in KNOWN_USB_VIDS]
    if not ports:
        semua = ", ".join(f"{p.device} ({p.description})" for p in list_ports.comports()) or "tidak ada"
        return None, f"Tidak ada port USB-Serial ESP32 (CP210x/CH340/FTDI/Espressif). Port terdeteksi: {semua}"

    errors = []
    for dev in ports:
        users = port_users(dev)
        if users:
            errors.append(f"{dev} sedang dipakai program lain -> hentikan dulu:\n      " + "\n      ".join(users))
            continue
        try:
            ser = serial.Serial(dev, baud, timeout=0.2, write_timeout=1.0)
        except (serial.SerialException, OSError) as e:
            errors.append(f"{dev}: {e}")
            continue

        link = Link(ser)
        info = {'port': dev, 'firmware': None, 'lcd': None}
        got_pong, t_start = False, time.time()
        try:
            # Membuka port biasanya me-reset ESP32: kirim PING sampai firmware selesai boot & membalas
            while time.time() - t_start < 6 and not got_pong:
                link.send("PING")
                for line in link.collect(0.5):
                    if line.startswith("READY:LCD_OK"):
                        info['lcd'] = line.split("_OK_", 1)[-1] if "_OK_" in line else "OK"
                    elif line.startswith("READY:"):
                        info['firmware'] = line.split(":", 1)[1]
                    elif line == "ERR:LCD_NOT_FOUND":
                        info['lcd'] = "TIDAK DITEMUKAN"
                    elif line == "PONG":
                        got_pong = True
            if not got_pong:
                link.close()
                errors.append(f"{dev}: tidak ada balasan PONG dalam 6 detik (firmware belum di-flash / baud salah?)")
                continue

            # Kenali firmware: firmware uji menjawab INFO, firmware gerbang membalas ERR:UNKNOWN:INFO
            link.send("INFO")
            _, line = link.wait(lambda l: l.startswith("INFO:") or l == "ERR:UNKNOWN:INFO", 1.5)
            if line and line.startswith("INFO:"):
                parts = line.split(":")
                info['firmware'] = parts[1]
                if len(parts) > 3:
                    info['lcd'] = parts[3] if parts[3] != "NONE" else "TIDAK DITEMUKAN"
            elif line == "ERR:UNKNOWN:INFO" and not info['firmware']:
                info['firmware'] = "SMART_GATE (firmware gerbang)"
            info['firmware'] = info['firmware'] or "tidak diketahui"
            info['lcd'] = info['lcd'] or "tidak diketahui (ESP32 tidak ter-reset saat port dibuka)"
            link.drain()
            link.keepalive.set()
            return link, info
        except ConnectionError as e:
            link.close()
            errors.append(f"{dev}: {e}")
    return None, "\n   ".join(errors)


# ── Program uji ──────────────────────────────────────────────────────────
class UjiLCD:
    def __init__(self, link, info, cols, rows, auto):
        self.link, self.info = link, info
        self.cols, self.rows, self.auto = cols, rows, auto
        self.is_uji = info['firmware'].startswith(UJI_FW)
        self.results = []          # (nama, status, detail); status: LULUS / GAGAL / DILIHAT / LEWAT / PERINGATAN

    # Tampilan
    def _safe(self, text):
        return "".join(c if 32 <= ord(c) < 127 and c != '|' else ' ' for c in str(text))

    def show(self, lines):
        """Kirim layar ke LCD. Firmware uji: tunggu OK:LCD dan kembalikan waktu tulis I2C (ms);
        firmware gerbang: kembalikan 0 (tidak ada balasan). None = tidak ada balasan / error."""
        lines = (list(lines) + [""] * self.rows)[:self.rows]
        payload = "|".join(self._safe(l)[:self.cols].ljust(self.cols) for l in lines)
        self.link.send("LCD:" + payload)
        if not self.is_uji:
            return 0
        _, line = self.link.wait(lambda l: l.startswith("OK:LCD:") or l == "ERR:LCD_NOT_FOUND", 1.0)
        if line and line.startswith("OK:LCD:"):
            return int(line.split(":")[2] or 0)
        return None

    def print_screen(self, lines):
        border = "+" + "-" * self.cols + "+"
        print("      " + border)
        for l in (list(lines) + [""] * self.rows)[:self.rows]:
            print("      |" + self._safe(l)[:self.cols].ljust(self.cols) + "|")
        print("      " + border)

    def ask(self, question):
        """y/n dari pengguna; None pada mode --auto (hasil dicatat DILIHAT)."""
        if self.auto:
            return None
        while True:
            ans = input(f"   >> {question} [y/n]: ").strip().lower()
            if ans in ('y', 'ya', 'n', 'no', 'tidak', 't'):
                return ans in ('y', 'ya')

    def record(self, name, status, detail=""):
        self.results.append((name, status, detail))
        print(f"   [{status}] {name}" + (f" - {detail}" if detail else ""))

    def visual(self, name, lines, question, extra=""):
        print(f"\n== {name}")
        ms = self.show(lines)
        self.print_screen(lines)
        if self.is_uji and ms is None:
            self.record(name, "GAGAL", "ESP32 tidak membalas OK:LCD")
            return
        ans = self.ask(question)
        status = "DILIHAT" if ans is None else ("LULUS" if ans else "GAGAL")
        self.record(name, status, extra)

    # Tahap
    def step_size(self):
        name = f"Ukuran LCD {self.cols}x{self.rows}"
        if self.is_uji:
            self.link.send(f"SIZE:{self.cols}x{self.rows}")
            _, line = self.link.wait(lambda l: l.startswith("OK:SIZE") or l in ("ERR:SIZE", "ERR:LCD_NOT_FOUND"), 2.0)
            if line == f"OK:SIZE:{self.cols}x{self.rows}":
                self.record(name, "LULUS", "LCD diinisialisasi ulang oleh firmware uji")
                return True
            self.record(name, "GAGAL", f"balasan ESP32: {line or 'tidak ada'}")
            return False
        detail = "firmware gerbang memakai ukuran yang dikompilasi (LCD_COLS/LCD_ROWS di .ino, saat ini 16x2)"
        if (self.cols, self.rows) != (16, 2):
            detail += "; baris 3-4 tidak akan tampil -> flash uji_lcd_esp32.ino untuk 20x4"
        self.record(name, "PERINGATAN" if (self.cols, self.rows) != (16, 2) else "LULUS", detail)
        return True

    def step_ping(self):
        print(f"\n== Waktu respons PING -> PONG ({PING_COUNT}x)")
        self.link.keepalive.clear()
        time.sleep(0.2)
        self.link.drain()
        rtt, lost = [], 0
        for _ in range(PING_COUNT):
            t0 = self.link.send("PING")
            t, line = self.link.wait(lambda l: l == "PONG", 1.0)
            if line:
                rtt.append((t - t0) * 1000)
            else:
                lost += 1
            time.sleep(0.05)
        self.link.keepalive.set()
        if not rtt:
            self.record("PING -> PONG", "GAGAL", f"0/{PING_COUNT} dibalas")
            return
        detail = (f"rata-rata {sum(rtt) / len(rtt):.1f} ms, min {min(rtt):.1f} ms, maks {max(rtt):.1f} ms, "
                  f"hilang {lost}/{PING_COUNT}")
        self.record("PING -> PONG", "LULUS" if lost == 0 else "GAGAL", detail)

    def step_screens(self):
        c, r = self.cols, self.rows
        self.visual("Tepi & urutan baris",
                    ["[" + f"BARIS {i + 1}".center(c - 2) + "]" for i in range(r)],
                    f"Tampil BARIS 1 s/d {r} berurutan, '[' di kolom paling kiri dan ']' paling kanan?")
        ruler = "".join(str((i + 1) % 10) for i in range(c))
        self.visual("Penggaris kolom", [ruler] * r,
                    f"Setiap baris menampilkan {ruler} lengkap ({c} kolom, tidak terpotong)?")

        print("\n== Semua karakter ASCII (tanpa '|', karena dipakai pemisah baris protokol)")
        chars = "".join(chr(i) for i in range(32, 127) if chr(i) != '|')
        page, ok = c * r, True
        for p in range(0, len(chars), page):
            chunk = chars[p:p + page]
            lines = [chunk[i:i + c] for i in range(0, len(chunk), c)]
            print(f"   halaman {p // page + 1}/{-(-len(chars) // page)}:")
            if self.show(lines) is None and self.is_uji:
                ok = False
            self.print_screen(lines)
            time.sleep(3)
        if not ok:
            self.record("Semua karakter", "GAGAL", "ESP32 tidak membalas OK:LCD")
        else:
            ans = self.ask("Semua halaman di LCD sama persis dengan yang dicetak di terminal?")
            self.record("Semua karakter", "DILIHAT" if ans is None else ("LULUS" if ans else "GAGAL"))

        self.visual("Layar penuh", ["#" * c] * r,
                    "Semua kotak terisi '#' (tidak ada kotak mati, kontras cukup terbaca)?")

        print(f"\n== Penghitung cepat ({COUNTER_STEPS} pembaruan, {COUNTER_HZ}x per detik)")
        acks, i2c = 0, []
        for i in range(1, COUNTER_STEPS + 1):
            last = f"{i2c[-1]} ms" if i2c else "-"
            if r >= 4:
                lines = ["UJI PENGHITUNG", f"{i:03d} / {COUNTER_STEPS}", f"TULIS I2C: {last}",
                         "#" * (i * c // COUNTER_STEPS)]
            else:
                lines = [f"HITUNG {i:03d}/{COUNTER_STEPS}", "#" * (i * c // COUNTER_STEPS)]
            ms = self.show(lines)
            if ms is not None and self.is_uji:
                acks += 1
                i2c.append(ms)
            print(f"\r   terkirim {i}/{COUNTER_STEPS}", end="", flush=True)
            time.sleep(1 / COUNTER_HZ)
        print()
        if self.is_uji:
            detail = f"dibalas {acks}/{COUNTER_STEPS}" + (
                f", tulis I2C rata-rata {sum(i2c) / len(i2c):.0f} ms, maks {max(i2c)} ms" if i2c else "")
            if acks < COUNTER_STEPS:
                self.record("Penghitung cepat", "GAGAL", detail)
                return
        else:
            self.link.send("PING")
            _, pong = self.link.wait(lambda l: l == "PONG", 1.5)
            detail = "firmware gerbang tidak membalas LCD; ESP32 masih membalas PING" if pong else "ESP32 tidak membalas PING"
            if not pong:
                self.record("Penghitung cepat", "GAGAL", detail)
                return
        ans = self.ask(f"Angka naik mulus sampai {COUNTER_STEPS}/{COUNTER_STEPS} dan bar '#' terisi penuh?")
        self.record("Penghitung cepat", "DILIHAT" if ans is None else ("LULUS" if ans else "GAGAL"), detail)

        print("\n== LCDCLR (bersihkan layar)")
        self.link.send("LCDCLR")
        if self.is_uji:
            _, line = self.link.wait(lambda l: l == "OK:LCDCLR", 1.0)
            if not line:
                self.record("LCDCLR", "GAGAL", "ESP32 tidak membalas OK:LCDCLR")
                return
        ans = self.ask("LCD sekarang kosong?")
        self.record("LCDCLR", "DILIHAT" if ans is None else ("LULUS" if ans else "GAGAL"))

    def step_watchdog(self):
        print(f"\n== Watchdog firmware: Mini PC berhenti mengirim data selama {WATCHDOG_WAIT} detik")
        self.show(["UJI WATCHDOG", f"TUNGGU {WATCHDOG_WAIT} DETIK"] + (["LCD AKAN BERUBAH", "JADI OFFLINE"] if self.rows >= 4 else []))
        self.link.keepalive.clear()
        for s in range(WATCHDOG_WAIT, 0, -1):
            print(f"\r   diam... {s} detik ", end="", flush=True)
            time.sleep(1)
        print()
        seen = self.ask("LCD menampilkan 'MINI PC OFFLINE!'?")
        self.link.keepalive.set()
        self.link.drain()
        self.link.send("PING")
        _, pong = self.link.wait(lambda l: l == "PONG", 1.5)
        ms = self.show(["KEMBALI ONLINE", "KOMUNIKASI OK"])
        if not pong or (self.is_uji and ms is None):
            self.record("Watchdog & pulih", "GAGAL", "ESP32 tidak merespons setelah Mini PC kembali mengirim")
            return
        back = self.ask("LCD kembali menampilkan 'KEMBALI ONLINE'?")
        answers = [a for a in (seen, back) if a is not None]
        status = "DILIHAT" if not answers else ("LULUS" if all(answers) else "GAGAL")
        self.record("Watchdog & pulih", status, "ESP32 kembali membalas PING")

    def finish(self):
        checked = [s for _, s, _ in self.results if s in ("LULUS", "GAGAL")]
        passed = checked.count("LULUS")
        failed = checked.count("GAGAL")
        lines = ["UJI SELESAI", f"LULUS {passed}/{len(checked)}"]
        if self.rows >= 4:
            lines += [f"MODE {self.cols}x{self.rows}", time.strftime("%d-%m-%Y %H:%M")]
        try:
            self.show(lines)
        except ConnectionError:
            pass

        print("\n" + "=" * 72)
        print(f" HASIL UJI LCD {self.cols}x{self.rows}  |  port {self.info['port']}  |  firmware {self.info['firmware']}")
        print("=" * 72)
        for name, status, detail in self.results:
            print(f" {status:<10} {name:<24} {detail}")
        print("-" * 72)
        print(f" LULUS {passed}, GAGAL {failed}" + (
            f", DILIHAT {sum(1 for _, s, _ in self.results if s == 'DILIHAT')} (mode --auto, cek LCD sendiri)"
            if self.auto else ""))
        return failed == 0


def main():
    ap = argparse.ArgumentParser(description="Uji komunikasi Mini PC <-> ESP32 lewat LCD I2C (16x2 / 20x4)")
    ap.add_argument('--mode', choices=['16x2', '20x4'],
                    default=f"{getattr(config, 'LCD_COLS', 16)}x{getattr(config, 'LCD_ROWS', 2)}",
                    help="ukuran LCD (default: LCD_COLS x LCD_ROWS di config.py)")
    ap.add_argument('--port', default=getattr(config, 'ESP32_PORT', 'AUTO'),
                    help="AUTO / COM5 / /dev/ttyUSB0 (default: ESP32_PORT di config.py)")
    ap.add_argument('--baud', type=int, default=getattr(config, 'ESP32_BAUD', 115200))
    ap.add_argument('--auto', action='store_true', help="tanpa pertanyaan y/n (hasil visual dicatat DILIHAT)")
    ap.add_argument('--tanpa-watchdog', action='store_true', help="lewati uji 'MINI PC OFFLINE!' (7 detik)")
    args = ap.parse_args()
    cols, rows = map(int, args.mode.split('x'))

    print(f"== Koneksi & handshake (port {args.port}, {args.baud} baud)")
    link, info = connect(args.port, args.baud)
    if link is None:
        print(f"   [GAGAL] {info}")
        print("   Cek: kabel USB data (bukan hanya daya), firmware sudah di-flash, izin grup 'dialout' (Linux),"
              " dan main.py / Live ALPR tidak sedang berjalan.")
        return 1

    uji = UjiLCD(link, info, cols, rows, args.auto)
    lcd_ok = "TIDAK" not in info['lcd']
    uji.record("Koneksi & handshake", "LULUS", f"port {info['port']}, firmware {info['firmware']}")
    uji.record("LCD terdeteksi ESP32", "LULUS" if lcd_ok else "GAGAL", f"alamat I2C {info['lcd']}")
    try:
        if not lcd_ok:
            print("   LCD tidak terdeteksi di bus I2C: cek kabel SDA GPIO21 / SCL GPIO22, catu 5V, dan GND bersama.")
        elif uji.step_size():
            uji.step_ping()
            uji.step_screens()
            if args.tanpa_watchdog:
                uji.record("Watchdog & pulih", "LEWAT", "--tanpa-watchdog")
            else:
                uji.step_watchdog()
    except ConnectionError as e:
        uji.record("Koneksi", "GAGAL", f"terputus di tengah uji: {e}")
    except KeyboardInterrupt:
        print("\n[INFO] Uji dibatalkan.")
        try:
            uji.show(["UJI DIBATALKAN"])
        except ConnectionError:
            pass
        link.close()
        return 1
    ok = uji.finish()
    link.close()
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
