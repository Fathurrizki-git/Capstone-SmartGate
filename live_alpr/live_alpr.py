"""
Live ALPR: membaca plat terus-menerus dari webcam, ditonton lewat browser (tema dashboard Smart Gate).

    python scripts/live_alpr.py                 # buka http://<IP-Mini-PC>:5051
    python scripts/live_alpr.py --port 5052 --width 1280 --stable 3

Tampilan di atas video:
    kiri atas   : FPS model ALPR
    kanan atas  : plat terakhir yang stabil, skor, latensi, jumlah plat, kamera, riwayat
    kanan bawah : versi ALPR
Kotak plat digambar di video: hijau = sesuai pola plat Indonesia, merah = tidak sesuai.

Tampilan halaman ada di web_dashboard/live_alpr.html (memakai aset web_dashboard/img).
Tidak mengubah main.py. Hentikan dulu service gerbang agar kamera tidak dipakai dua program:
    sudo systemctl stop smartgate
"""

import argparse
import json
import os
import sys
import threading
import time
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Folder utama proyek ke path agar config & smart_gate dapat di-import
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2

import config
from smart_gate.vision.camera import CameraStream
from smart_gate.vision.local_alpr import LocalALPR

IMG_DIR = os.path.join(ROOT, 'web_dashboard', 'img')
GREEN, RED = (116, 181, 1), (60, 56, 255)          # BGR dari --green #01b574 dan --red #ff383c


def alpr_version(runtime='ONNX Runtime CPU'):
    """Versi ALPR: config.ALPR_VERSION bila ada, selain itu tanggal file model."""
    v = getattr(config, 'ALPR_VERSION', None)
    model_dir = getattr(config, 'ALPR_MODEL_DIR', 'alpr_model/weights26')
    det = os.path.join(model_dir if os.path.isabs(model_dir) else os.path.join(ROOT, model_dir), 'plate_det.onnx')
    stamp = time.strftime('%d %b %Y', time.localtime(os.path.getmtime(det))) if os.path.exists(det) else '-'
    return {'version': v or f'model {stamp}', 'engine': 'YOLO + PP-OCRv5', 'runtime': runtime}


class LiveALPR:
    def __init__(self, width=960, stable=3, window=5):
        self.alpr = LocalALPR()
        self.cam = CameraStream()
        self.width, self.stable = width, stable
        self.lock = threading.Lock()
        self.results, self.ms, self.fps = [], 0, 0.0
        self.recent = deque(maxlen=window)
        self.last = None                       # plat stabil terakhir: {plate, conf, time}
        self.history = deque(maxlen=6)
        self.started = time.time()
        self.version = alpr_version(getattr(self.alpr, 'runtime', 'ONNX Runtime CPU'))
        self.stop = threading.Event()
        threading.Thread(target=self._detect_loop, daemon=True).start()

    # ── Thread pembaca plat: memproses frame terbaru secepat CPU mampu ──
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
                self.results = res.get('results', [])
                self.ms = res.get('usage', {}).get('local_ms', 0)
                self.fps = 0.8 * self.fps + 0.2 * (1.0 / max(now - t_prev, 1e-3)) if self.fps else 1.0 / max(now - t_prev, 1e-3)
                self._update_stable(self.results, now)
            t_prev = now

    def _update_stable(self, results, now):
        """Plat dianggap stabil bila terbaca sama di >= `stable` dari pembacaan terakhir."""
        best = next((r['plate_text'] for r in results if r.get('valid')), None)
        self.recent.append(best)
        counts = Counter(p for p in self.recent if p)
        if not counts:
            return
        plate, n = counts.most_common(1)[0]
        if n < self.stable:
            return
        conf = next((r['confidence'] for r in results if r['plate_text'] == plate), self.last['conf'] if self.last else 0)
        is_new = not self.last or plate != self.last['plate'] or now - self.last['time'] > 10
        self.last = {'plate': plate, 'conf': round(float(conf), 2), 'time': now}
        if is_new:
            self.history.appendleft({'plate': plate, 'time': time.strftime('%H:%M:%S', time.localtime(now))})
            print(f"[{time.strftime('%H:%M:%S')}] PLAT: {plate:12s} (stabil {n}/{len(self.recent)}, "
                  f"skor OCR {conf:.2f}, {self.ms} ms/frame)", flush=True)

    def status(self):
        with self.lock:
            last = dict(self.last) if self.last else None
            if last:
                last['age'] = round(time.time() - last.pop('time'), 1)
            return {
                'fps': round(self.fps, 1), 'ms': self.ms, 'plates_in_frame': len(self.results),
                'last': last, 'history': list(self.history),
                'camera': self.cam.name or '-', 'resolution': 'x'.join(map(str, self.cam.resolution or ())) or '-',
                'camera_ok': self.cam.read() is not None,
                'uptime': int(time.time() - self.started), 'alpr': self.version,
            }

    # ── Frame video dengan kotak plat ──
    def annotated_jpeg(self):
        frame = self.cam.read()
        if frame is None:
            return None
        img = frame.copy()
        with self.lock:
            results = list(self.results)
        s = max(img.shape[1] / 1280.0, 0.5)
        thick = max(2, int(2 * s))
        for r in results:
            b = r['bbox']
            col = GREEN if r.get('valid') else RED
            cv2.rectangle(img, (b['xmin'], b['ymin']), (b['xmax'], b['ymax']), col, max(2, int(3 * s)))
            label = f"{r['plate_text']}  {r['confidence']:.2f}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.9 * s, thick)
            y = max(th + 12, b['ymin'] - 10)
            cv2.rectangle(img, (b['xmin'], y - th - 10), (b['xmin'] + tw + 12, y + 8), col, -1)
            cv2.putText(img, label, (b['xmin'] + 6, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9 * s, (255, 255, 255), thick, cv2.LINE_AA)
        if img.shape[1] > self.width:
            img = cv2.resize(img, (self.width, int(img.shape[0] * self.width / img.shape[1])))
        ok, jpg = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 72])
        return jpg.tobytes() if ok else None

    def close(self):
        self.stop.set()
        self.cam.stop()


HTML_FILE = os.path.join(ROOT, 'live_alpr', 'live_alpr.html')   # tampilan halaman (HTML + CSS + JS)


def load_page():
    """Dibaca setiap halaman dibuka, jadi perubahan HTML cukup di-refresh tanpa restart skrip."""
    with open(HTML_FILE, 'rb') as f:
        return f.read()


def make_handler(live):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body, ctype, cache=True):
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            if not cache:
                self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split('?')[0]
            if path in ('/', '/live_alpr.html'):
                try:
                    self._send(load_page(), 'text/html; charset=utf-8', cache=False)
                except FileNotFoundError:
                    self.send_error(500, f'File tampilan tidak ditemukan: {HTML_FILE}')
            elif path == '/status':
                self._send(json.dumps(live.status()).encode(), 'application/json', cache=False)
            elif path.startswith('/img/'):
                name = os.path.basename(path)                       # hanya file di web_dashboard/img
                full = os.path.join(IMG_DIR, name)
                if name.lower().endswith(('.png', '.jpg', '.svg')) and os.path.isfile(full):
                    ctype = 'image/svg+xml' if name.endswith('.svg') else 'image/png' if name.endswith('.png') else 'image/jpeg'
                    with open(full, 'rb') as f:
                        self._send(f.read(), ctype)
                else:
                    self.send_error(404)
            elif path == '/stream':
                self.send_response(200)
                self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try:
                    while not live.stop.is_set():
                        jpg = live.annotated_jpeg()
                        if jpg is None:
                            time.sleep(0.2)
                            continue
                        self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                                         + str(len(jpg)).encode() + b'\r\n\r\n' + jpg + b'\r\n')
                        time.sleep(1 / 15)          # tayangan +-15 fps
                except (BrokenPipeError, ConnectionResetError):
                    pass                            # browser ditutup
            else:
                self.send_error(404)
    return Handler


def local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return 'localhost'


def main():
    ap = argparse.ArgumentParser(description='Live ALPR dari webcam (tonton di browser)')
    ap.add_argument('--port', type=int, default=5051, help='port web (default 5051, beda dari dashboard 5050)')
    ap.add_argument('--width', type=int, default=960, help='lebar video di browser (px)')
    ap.add_argument('--stable', type=int, default=3, help='berapa pembacaan sama (dari 5 terakhir) sebelum plat dianggap stabil')
    args = ap.parse_args()

    live = LiveALPR(width=args.width, stable=args.stable)
    server = ThreadingHTTPServer(('0.0.0.0', args.port), make_handler(live))
    server.daemon_threads = True
    print(f'\n[OK] Live ALPR berjalan. Buka di browser: http://{local_ip()}:{args.port}   (Ctrl+C untuk berhenti)\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[STOP] Menghentikan live ALPR...')
    finally:
        server.server_close()
        live.close()


if __name__ == '__main__':
    main()
