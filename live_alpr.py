"""
Demo ALPR terus-menerus dari webcam, ditonton lewat browser.

    python scripts/live_alpr.py                 # buka http://<IP-Mini-PC>:5051
    python scripts/live_alpr.py --port 5052 --width 1280 --stable 3

- Webcam dibuka dengan CameraStream (pemilihan kamera sama seperti main.py).
- Setiap frame terbaru dibaca LocalALPR (YOLOv8n + PP-OCRv5 ONNX) secepat CPU mampu.
- Video ditayangkan lancar; kotak + teks plat diperbarui setiap kali model selesai membaca.
  Hijau = sesuai pola plat Indonesia, merah = tidak sesuai.
- Terminal hanya mencetak plat yang STABIL (sama di beberapa pembacaan berturut-turut).

Tidak mengubah main.py. Hentikan dulu service gerbang agar kamera tidak dipakai dua program:
    sudo systemctl stop smartgate
"""

import argparse
import os
import sys
import threading
import time
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Folder utama proyek ke path agar config & smart_gate dapat di-import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from smart_gate.vision.camera import CameraStream
from smart_gate.vision.local_alpr import LocalALPR

PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live ALPR</title><style>body{margin:0;background:#111;color:#eee;font-family:sans-serif;text-align:center}
img{max-width:100%;height:auto}h3{margin:10px 0;font-weight:500}</style></head>
<body><h3>Live ALPR &middot; Smart Gate FT UNTIRTA</h3><img src="/stream"></body></html>"""


class LiveALPR:
    def __init__(self, width=960, stable=3, window=5):
        self.alpr = LocalALPR()
        self.cam = CameraStream()
        self.width = width
        self.stable = stable
        self.lock = threading.Lock()
        self.results, self.ms, self.fps = [], 0, 0.0
        self.recent = deque(maxlen=window)
        self.last_printed, self.last_print_t = None, 0.0
        self.stop = threading.Event()
        threading.Thread(target=self._detect_loop, daemon=True).start()

    # ── Thread pembaca plat: memproses frame terbaru secepat mungkin ──
    def _detect_loop(self):
        t_prev = time.time()
        while not self.stop.is_set():
            frame = self.cam.read()
            if frame is None:
                time.sleep(0.2)
                continue
            res = self.alpr.recognize(frame)
            now = time.time()
            with self.lock:
                self.results = res.get("results", [])
                self.ms = res.get("usage", {}).get("local_ms", 0)
                self.fps = 0.8 * self.fps + 0.2 * (1.0 / max(now - t_prev, 1e-3))
            t_prev = now
            self._report(self.results)

    def _report(self, results):
        """Cetak plat bila terbaca sama di >= `stable` dari pembacaan terakhir."""
        best = next((r["plate_text"] for r in results if r.get("valid")), None)
        self.recent.append(best)
        counts = Counter(p for p in self.recent if p)
        if not counts:
            return
        plate, n = counts.most_common(1)[0]
        now = time.time()
        if n >= self.stable and (plate != self.last_printed or now - self.last_print_t > 10):
            conf = next((r["confidence"] for r in results if r["plate_text"] == plate), 0)
            print(f"[{time.strftime('%H:%M:%S')}] PLAT: {plate:12s} (stabil {n}/{len(self.recent)}, "
                  f"skor OCR {conf:.2f}, {self.ms} ms/frame)", flush=True)
            self.last_printed, self.last_print_t = plate, now

    # ── Frame beranotasi untuk browser ──
    def annotated_jpeg(self):
        frame = self.cam.read()
        if frame is None:
            return None
        img = frame.copy()
        with self.lock:
            results, ms, fps = list(self.results), self.ms, self.fps
        s = max(img.shape[1] / 1280.0, 0.5)
        for r in results:
            b = r["bbox"]
            col = (0, 200, 0) if r.get("valid") else (0, 0, 255)
            cv2.rectangle(img, (b["xmin"], b["ymin"]), (b["xmax"], b["ymax"]), col, max(2, int(3 * s)))
            label = f'{r["plate_text"]}  {r["confidence"]:.2f}'
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 1.0 * s, max(2, int(2 * s)))
            y = max(th + 10, b["ymin"] - 10)
            cv2.rectangle(img, (b["xmin"], y - th - 8), (b["xmin"] + tw + 8, y + 6), col, -1)
            cv2.putText(img, label, (b["xmin"] + 4, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0 * s, (255, 255, 255),
                        max(2, int(2 * s)), cv2.LINE_AA)
        info = f"ALPR {fps:.1f} FPS | {ms} ms/frame | plat: {len(results)}"
        cv2.putText(img, info, (12, int(36 * s)), cv2.FONT_HERSHEY_SIMPLEX, 0.9 * s, (0, 0, 0), max(4, int(5 * s)), cv2.LINE_AA)
        cv2.putText(img, info, (12, int(36 * s)), cv2.FONT_HERSHEY_SIMPLEX, 0.9 * s, (255, 255, 255), max(2, int(2 * s)), cv2.LINE_AA)
        if img.shape[1] > self.width:
            img = cv2.resize(img, (self.width, int(img.shape[0] * self.width / img.shape[1])))
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return jpg.tobytes() if ok else None

    def close(self):
        self.stop.set()
        self.cam.stop()


def make_handler(live):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(PAGE)))
                self.end_headers()
                self.wfile.write(PAGE)
            elif self.path.startswith("/stream"):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while not live.stop.is_set():
                        jpg = live.annotated_jpeg()
                        if jpg is None:
                            time.sleep(0.2)
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                        time.sleep(1 / 15)          # tayangan ±15 fps
                except (BrokenPipeError, ConnectionResetError):
                    pass                            # browser ditutup
            else:
                self.send_error(404)
    return Handler


def local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "localhost"


def main():
    ap = argparse.ArgumentParser(description="Demo ALPR terus-menerus dari webcam (tonton di browser)")
    ap.add_argument("--port", type=int, default=5051, help="port web (default 5051, beda dari dashboard 5050)")
    ap.add_argument("--width", type=int, default=960, help="lebar video di browser (px)")
    ap.add_argument("--stable", type=int, default=3, help="berapa pembacaan sama (dari 5 terakhir) sebelum plat dicetak")
    args = ap.parse_args()

    live = LiveALPR(width=args.width, stable=args.stable)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(live))
    server.daemon_threads = True
    print(f"\n[OK] Live ALPR berjalan. Buka di browser: http://{local_ip()}:{args.port}   (Ctrl+C untuk berhenti)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[STOP] Menghentikan live ALPR...")
    finally:
        server.shutdown()
        live.close()


if __name__ == "__main__":
    main()
