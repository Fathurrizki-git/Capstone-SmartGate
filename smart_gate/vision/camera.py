"""
Modul Kamera Webcam USB (Thread Capture Terpisah)

Webcam dibaca terus-menerus di thread background sehingga:
- Live stream Web Dashboard TIDAK membeku saat verifikasi ALPR, menunggu tap RFID,
  atau menunggu kendaraan melintas (loop utama boleh sibuk / sleep).
- Frame yang diambil untuk ALPR selalu frame terbaru (tidak ada frame basi di buffer webcam).
- Kamera otomatis dibuka ulang jika kabel webcam terlepas.

Pemilihan webcam berdasarkan NAMA (config.CAMERA_NAME), karena nomor index bisa
bergeser saat webcam dicabut/dipasang atau aplikasi kamera virtual (OBS, Cast) terpasang.

Deteksi webcam yang tersambung:
    python -m smart_gate.vision.camera
"""

import os
import sys
import time
import threading

# Harus diset SEBELUM cv2 di-import: tanpa ini backend MSMF butuh ~30 detik untuk membuka webcam
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import cv2
import config

try:
    from cv2_enumerate_cameras import enumerate_cameras
    HAS_ENUMERATE = True
except ImportError:
    HAS_ENUMERATE = False


def get_backend() -> int:
    """
    Backend OpenCV sesuai OS & config.CAMERA_BACKEND.
    Windows default MSMF: pada sebagian webcam (mis. NYK NEMESIS) DirectShow mengabaikan
    permintaan MJPG sehingga 1080p jatuh ke YUY2 ~1 fps.
    """
    if sys.platform.startswith('win'):
        if str(getattr(config, 'CAMERA_BACKEND', 'MSMF')).upper() == 'DSHOW':
            return cv2.CAP_DSHOW
        return cv2.CAP_MSMF
    if sys.platform.startswith('linux'):
        return cv2.CAP_V4L2
    return cv2.CAP_ANY


def list_cameras(backend=None):
    """
    Daftar webcam pada backend: [{"index", "name", "vid", "pid", "virtual"}].
    Kamera virtual (OBS, Cast, dsb.) tidak memiliki VID/PID USB.
    """
    if not HAS_ENUMERATE:
        return []
    try:
        cams = enumerate_cameras(backend if backend is not None else get_backend())
    except Exception:
        return []
    return [
        {"index": c.index, "name": c.name, "vid": c.vid, "pid": c.pid, "virtual": not c.vid}
        for c in cams
    ]


def _candidate_indices():
    """Urutan index yang dicoba: webcam sesuai CAMERA_NAME -> webcam fisik lain -> CAMERA_INDEX."""
    cams = list_cameras()
    wanted = str(getattr(config, 'CAMERA_NAME', '') or '').strip().lower()
    candidates = []

    if cams:
        if wanted:
            matched = [c for c in cams if wanted in c["name"].lower()]
            if matched:
                candidates += [(c["index"], c["name"]) for c in matched]
            else:
                names = ", ".join(c["name"] for c in cams)
                print(f"[WARN] Webcam '{config.CAMERA_NAME}' tidak ditemukan. Terdeteksi: {names}")
        candidates += [(c["index"], c["name"]) for c in cams if not c["virtual"]]
    else:
        # Tanpa cv2-enumerate-cameras: coba index manual (tidak dapat menyaring kamera virtual)
        candidates += [(i, None) for i in (config.CAMERA_INDEX, 0, 1, 2)]

    seen = set()
    return [c for c in candidates if not (c[0] in seen or seen.add(c[0]))]


def _open_capture(idx: int, backend: int, width=None, height=None):
    """Buka satu webcam dengan MJPG + resolusi dari config. Return VideoCapture atau None."""
    cap = cv2.VideoCapture(idx, backend)
    if not cap.isOpened():
        cap.release()
        return None
    # FOURCC harus diset sebelum resolusi: tanpa MJPG kebanyakan webcam hanya ~1-5 fps di 1080p
    if getattr(config, 'CAMERA_MJPG', True):
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width or config.CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height or config.CAMERA_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, getattr(config, 'CAMERA_FPS', 30))
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


class CameraStream:
    """Pembaca webcam USB di thread background dengan frame terbaru yang thread-safe."""

    def __init__(self, on_frame=None):
        self.on_frame = on_frame  # Callback tiap frame baru (misal: kirim ke live stream server)
        self.cap = None
        self.index = None
        self.name = None
        self.resolution = None
        self._frame = None
        self._frame_time = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()

        self._open()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    @property
    def is_opened(self) -> bool:
        return self.cap is not None and self.cap.isOpened()

    def _open(self) -> bool:
        """Buka Webcam USB (dipilih berdasarkan nama), format MJPG, dan resolusi dari config."""
        backend = get_backend()
        for idx, name in _candidate_indices():
            cap = _open_capture(idx, backend)
            if cap is None:
                continue

            ret, test_frame = cap.read()
            if ret and test_frame is not None:
                self.cap = cap
                self.index = idx
                self.name = name or f"index {idx}"
                self.resolution = (test_frame.shape[1], test_frame.shape[0])
                print(f"[OK] Webcam aktif: {self.name} (index {idx}, {self.resolution[0]}x{self.resolution[1]}).")
                if self.resolution != (config.CAMERA_WIDTH, config.CAMERA_HEIGHT):
                    print(f"[WARN] Webcam tidak mendukung {config.CAMERA_WIDTH}x{config.CAMERA_HEIGHT}, "
                          f"memakai resolusi maksimal kamera {self.resolution[0]}x{self.resolution[1]}.")
                return True
            cap.release()

        print("[WARN] Webcam USB belum terdeteksi. Silakan periksa koneksi webcam atau perizinan kamera.")
        return False

    def _capture_loop(self):
        failures = 0
        while not self._stop.is_set():
            if not self.is_opened:
                time.sleep(2.0)
                if not self._stop.is_set():
                    self._open()
                continue

            ret, frame = self.cap.read()
            if not ret or frame is None:
                failures += 1
                if failures >= 30:
                    print("[WARN] Webcam berhenti mengirim gambar. Membuka ulang kamera...")
                    self.cap.release()
                    self.cap = None
                    failures = 0
                time.sleep(0.05)
                continue

            failures = 0
            with self._lock:
                self._frame = frame
                self._frame_time = time.time()
            if self.on_frame:
                self.on_frame(frame)

    def read(self):
        """Ambil frame terbaru (tanpa menunggu). Return None jika belum ada frame."""
        with self._lock:
            return self._frame

    def read_fresh(self, timeout: float = 1.0):
        """Tunggu frame yang diambil SETELAH fungsi ini dipanggil (untuk capture ALPR)."""
        requested_at = time.time()
        deadline = requested_at + timeout
        while time.time() < deadline:
            with self._lock:
                if self._frame is not None and self._frame_time > requested_at:
                    return self._frame
            time.sleep(0.01)
        return self.read()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)  # Tunggu read() terakhir selesai sebelum kamera dilepas
        if self.cap is not None:
            self.cap.release()
            self.cap = None


def _measure_fps(cap, seconds=2.0) -> float:
    for _ in range(3):  # Lewati frame awal (auto-exposure menyesuaikan)
        cap.read()
    start, frames = time.time(), 0
    while time.time() - start < seconds:
        ok, _ = cap.read()
        frames += ok
    return frames / seconds


def detect_cameras():
    """Tampilkan seluruh webcam yang tersambung beserta resolusi & fps nyata (1080p MJPG)."""
    backend = get_backend()
    backend_name = {cv2.CAP_MSMF: "MSMF", cv2.CAP_DSHOW: "DSHOW", cv2.CAP_V4L2: "V4L2"}.get(backend, "ANY")
    print("=" * 72)
    print(f"  DETEKSI WEBCAM (backend {backend_name}, uji {config.CAMERA_WIDTH}x{config.CAMERA_HEIGHT} MJPG)")
    print("=" * 72)

    cams = list_cameras(backend)
    if not cams:
        if not HAS_ENUMERATE:
            print("[WARN] Paket cv2-enumerate-cameras belum terpasang (pip install -r requirements.txt).")
        cams = [{"index": i, "name": f"Kamera index {i}", "vid": None, "pid": None, "virtual": False} for i in range(4)]

    best = None
    for cam in cams:
        usb_id = f"USB {cam['vid']:04X}:{cam['pid']:04X}" if cam["vid"] else "VIRTUAL"
        label = f"[{cam['index']}] {cam['name']} ({usb_id})"
        if cam["virtual"]:
            print(f"  {label}\n      -> dilewati (kamera virtual, bukan webcam fisik)")
            continue

        cap = _open_capture(cam["index"], backend)
        ok, frame = cap.read() if cap is not None else (False, None)
        if not ok or frame is None:
            print(f"  {label}\n      -> tidak dapat dibuka (sedang dipakai aplikasi lain?)")
            if cap is not None:
                cap.release()
            continue
        fps = _measure_fps(cap)
        cap.release()
        w, h = frame.shape[1], frame.shape[0]
        print(f"  {label}\n      -> {w}x{h} @ {fps:.1f} fps")
        score = (w * h, fps)
        if best is None or score > best[0]:
            best = (score, cam["name"])

    print("-" * 72)
    current = getattr(config, 'CAMERA_NAME', '')
    print(f"  config.CAMERA_NAME saat ini : {current!r}")
    if best:
        print(f"  Rekomendasi (resolusi & fps tertinggi): CAMERA_NAME = \"{best[1]}\"")
    print("=" * 72)


if __name__ == "__main__":
    detect_cameras()
