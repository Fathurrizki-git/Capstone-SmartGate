"""
Modul Komunikasi Serial Mini PC <-> ESP32 (USB Serial 115200 baud)

ESP32 mengendalikan seluruh I/O fisik gerbang (Sensor IR 1 & 2, Push Button,
Servo Palang, Buzzer Flash, LCD I2C 20x4). Mini PC mengirim perintah dan
menerima status sensor melalui protokol teks per baris (diakhiri '\\n').

Perintah Mini PC -> ESP32:
  PING                       -> dibalas PONG (sekaligus keepalive)
  STATUS                     -> dibalas ST:<ir1>:<ir2>:<btn>
  SERVO:<derajat>            -> putar servo palang (0-180)
  BEEP:<durasi_ms>:<jumlah>  -> bunyi buzzer pendek berulang
  ALERT:<durasi_ms>          -> bunyi alarm panjang
  LCD:<b1>|<b2>|<b3>|<b4>    -> tulis 4 baris LCD 20x4
  LCDCLR                     -> bersihkan LCD

Pesan ESP32 -> Mini PC:
  READY:<nama_firmware>      -> ESP32 baru menyala / reset
  ST:<ir1>:<ir2>:<btn>       -> heartbeat status (1 = terdeteksi / ditekan)
  EVT:IR1:<0|1>, EVT:IR2:<0|1>, EVT:BTN:<0|1>  -> perubahan status (sudah di-debounce)
  PONG, ERR:<pesan>
"""

import time
import threading
import config

try:
    import serial
    from serial.tools import list_ports
    HAS_PYSERIAL = True
except ImportError:
    HAS_PYSERIAL = False

# VID chip USB-Serial yang umum dipakai board ESP32
KNOWN_USB_VIDS = {
    0x10C4: "CP210x",
    0x1A86: "CH340/CH9102",
    0x0403: "FTDI",
    0x303A: "Espressif USB",
}


class ESP32Link:
    """Koneksi serial ke ESP32 dengan thread pembaca status & auto-reconnect."""

    def __init__(self, port=None, baud=None):
        self.port_setting = port or getattr(config, 'ESP32_PORT', 'AUTO')
        self.baud = baud or getattr(config, 'ESP32_BAUD', 115200)
        self.heartbeat_timeout = getattr(config, 'ESP32_HEARTBEAT_TIMEOUT', 2.0)

        self.ser = None
        self.port = None
        self.firmware = None
        self.last_seen = 0.0
        self.on_ready = None  # Callback saat ESP32 reset (misal: tulis ulang LCD)

        self.ir_before = False
        self.ir_after = False
        self.button_down = False
        self._button_presses = 0

        self._write_lock = threading.Lock()
        self._event_lock = threading.Lock()
        self._stop = threading.Event()

        if not HAS_PYSERIAL:
            print("[WARN] Modul pyserial belum terpasang (pip install pyserial). ESP32 tidak dapat dihubungkan.")
            return

        if not self._connect():
            print("[WARN] ESP32 belum terdeteksi. Sistem akan mencoba menghubungkan ulang secara otomatis.")

        threading.Thread(target=self._reader_loop, daemon=True).start()

    @property
    def connected(self) -> bool:
        return self.ser is not None and (time.time() - self.last_seen) < self.heartbeat_timeout

    def _candidate_ports(self):
        if self.port_setting and str(self.port_setting).upper() != 'AUTO':
            return [self.port_setting]
        return [p.device for p in list_ports.comports() if p.vid in KNOWN_USB_VIDS]

    def _connect(self) -> bool:
        """Cari port ESP32 dan lakukan handshake PING/PONG."""
        for dev in self._candidate_ports():
            try:
                ser = serial.Serial(dev, self.baud, timeout=0.2, write_timeout=0.5)
            except (serial.SerialException, OSError):
                continue

            # Membuka port memicu auto-reset ESP32; tunggu firmware booting lalu membalas
            deadline = time.time() + 4.0
            while time.time() < deadline:
                try:
                    ser.write(b"PING\n")
                    line = ser.readline().decode('ascii', errors='ignore').strip()
                except (serial.SerialException, OSError):
                    break
                if line == "PONG" or line.startswith("READY") or line.startswith("ST:"):
                    self.ser = ser
                    self.port = dev
                    self.last_seen = time.time()
                    self._handle_line(line)
                    print(f"[OK] ESP32 terhubung pada port {dev} ({self.baud} baud).")
                    return True

            try:
                ser.close()
            except Exception:
                pass
        return False

    def _close(self):
        with self._write_lock:
            if self.ser is not None:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None

    def _reader_loop(self):
        last_ping = 0.0
        while not self._stop.is_set():
            if self.ser is None:
                time.sleep(2.0)
                if self._connect() and self.on_ready:
                    self.on_ready()
                continue
            try:
                now = time.time()
                if now - last_ping >= 1.0:
                    self.send("PING")
                    last_ping = now
                raw = self.ser.readline()
                if raw:
                    self._handle_line(raw.decode('ascii', errors='ignore').strip())
            except (serial.SerialException, OSError, AttributeError) as e:
                print(f"[WARN] Koneksi ESP32 terputus: {e}")
                self._close()

    def _handle_line(self, line: str):
        if not line:
            return
        self.last_seen = time.time()

        if line.startswith("ST:"):
            parts = line.split(":")
            if len(parts) >= 4:
                self.ir_before = parts[1] == "1"
                self.ir_after = parts[2] == "1"
                self.button_down = parts[3] == "1"
        elif line.startswith("EVT:"):
            parts = line.split(":")
            if len(parts) < 3:
                return
            name, active = parts[1], parts[2] == "1"
            if name == "IR1":
                self.ir_before = active
            elif name == "IR2":
                self.ir_after = active
            elif name == "BTN":
                self.button_down = active
                if active:
                    with self._event_lock:
                        self._button_presses += 1
        elif line.startswith("READY"):
            first_boot = self.firmware is None
            self.firmware = line.split(":", 1)[1] if ":" in line else "ESP32"
            if not first_boot:
                print("[WARN] ESP32 ter-reset. Menyinkronkan ulang tampilan LCD...")
                if self.on_ready:
                    self.on_ready()
        elif line.startswith("ERR"):
            print(f"[WARN] ESP32 melaporkan: {line}")

    def send(self, command: str) -> bool:
        """Kirim satu baris perintah ke ESP32."""
        if self.ser is None:
            return False
        try:
            with self._write_lock:
                self.ser.write((command + "\n").encode('ascii', errors='replace'))
            return True
        except (serial.SerialException, OSError, AttributeError):
            self._close()
            return False

    def pop_button_press(self) -> bool:
        """Ambil 1 event tekan Push Button (setiap tekanan hanya dihitung sekali)."""
        with self._event_lock:
            if self._button_presses > 0:
                self._button_presses -= 1
                return True
        return False

    def close(self):
        self._stop.set()
        self._close()
