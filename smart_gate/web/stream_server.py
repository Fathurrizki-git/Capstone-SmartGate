"""
MJPEG HTTP Streaming Server, Local Web Dashboard & Cloudflare Tunnel untuk Kamera ALPR Smart Parking Gate FT Untirta.
Menyiarkan live feed webcam langsung ke Web Dashboard secara real-time via Localhost (0 Delay), LAN WiFi, & HTTPS Cloudflare Tunnel.
"""

import os
import re
import sys
import time
import socket
import shutil
import mimetypes
import subprocess
import threading
from urllib.parse import unquote
from socketserver import ThreadingMixIn
from http.server import HTTPServer, BaseHTTPRequestHandler
import cv2
import numpy as np
from smart_gate import PROJECT_ROOT
from smart_gate.web import local_api

WEB_DASHBOARD_DIR = os.path.join(PROJECT_ROOT, "web_dashboard")
CLOUDFLARED_EXE = os.path.join(PROJECT_ROOT, "cloudflared.exe")


def get_local_ip():
    """Mendeteksi alamat IP lokal (WiFi/LAN) perangkat secara otomatis."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class StreamHub:
    """Hub penyimpanan frame terbaru dalam bentuk JPEG bytes untuk disiarkan ke client."""
    def __init__(self):
        self.cond = threading.Condition()
        self.latest_jpeg = None
        self.last_update = 0.0
        self.frame_id = 0
        self._placeholder_jpeg = None
        self.width = 640
        self.quality = 60
        self.min_interval = 0.0
        self._raw_cond = threading.Condition()
        self._raw_frame = None
        self._encoder_thread = None

    def configure(self, width=640, quality=60, max_fps=0):
        """Atur lebar, kualitas JPEG, dan batas fps stream (0 = ikuti fps kamera)."""
        self.width = int(width)
        self.quality = int(quality)
        self.min_interval = 1.0 / max_fps if max_fps else 0.0

    def update_frame(self, frame_bgr):
        """
        Terima frame dari thread kamera tanpa memprosesnya (non-blocking).
        Resize + encode JPEG dikerjakan thread encoder agar pembacaan kamera tidak melambat.
        """
        if frame_bgr is None:
            return
        with self._raw_cond:
            self._raw_frame = frame_bgr
            if self._encoder_thread is None:
                self._encoder_thread = threading.Thread(target=self._encode_loop, daemon=True)
                self._encoder_thread.start()
            self._raw_cond.notify()

    def _encode_loop(self):
        while True:
            with self._raw_cond:
                self._raw_cond.wait_for(lambda: self._raw_frame is not None)
                frame_bgr, self._raw_frame = self._raw_frame, None  # Selalu ambil frame paling baru

            wait = self.min_interval - (time.time() - self.last_update)
            if wait > 0:
                time.sleep(wait)
            try:
                # Optimasi Latensi: perkecil frame (misal 1080p -> 960px) agar ukuran JPEG kecil & cepat dikirim
                h, w = frame_bgr.shape[:2]
                if w > self.width:
                    stream_frame = cv2.resize(frame_bgr, (self.width, int(h * self.width / w)), interpolation=cv2.INTER_LINEAR)
                else:
                    stream_frame = frame_bgr

                ret, buf = cv2.imencode(".jpg", stream_frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                if ret:
                    with self.cond:
                        self.latest_jpeg = buf.tobytes()
                        self.last_update = time.time()
                        self.frame_id += 1
                        self.cond.notify_all()  # Bangunkan semua client MJPEG: frame langsung dikirim tanpa polling
            except Exception:
                pass

    def get_jpeg(self):
        with self.cond:
            return self.latest_jpeg

    def wait_next(self, last_id, timeout=1.0):
        """Tunggu frame yang lebih baru dari last_id. Return (frame_id, jpeg)."""
        with self.cond:
            self.cond.wait_for(lambda: self.frame_id != last_id, timeout=timeout)
            return self.frame_id, self.latest_jpeg

    def get_standby_placeholder(self):
        """Buat gambar standby JPEG jika belum ada frame kamera yang masuk."""
        if self._placeholder_jpeg is None:
            h, w = 480, 640
            img = np.zeros((h, w, 3), dtype=np.uint8)
            img[:] = (18, 12, 6)
            cv2.rectangle(img, (20, 20), (w - 20, h - 20), (55, 31, 26), 2)
            cv2.putText(img, "SMART PARKING GATE FT UNTIRTA", (70, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
            cv2.putText(img, "KAMERA ALPR STANDBY...", (180, 260), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (44, 217, 255), 1)
            ret, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            self._placeholder_jpeg = buf.tobytes() if ret else b''
        return self._placeholder_jpeg


GLOBAL_HUB = StreamHub()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Server HTTP multi-thread agar setiap koneksi berjalan paralel tanpa saling memblokir."""
    daemon_threads = True
    allow_reuse_address = True


class MJPEGHandler(BaseHTTPRequestHandler):
    """Handler HTTP untuk menyiarkan Web Dashboard Lokal, snapshot JPEG, multipart MJPEG stream, status, dan API lokal."""
    db = None  # GateDatabase untuk API dashboard lokal (/api/...), diset oleh VideoStreamServer

    def do_POST(self):
        clean_path = unquote(self.path.split('?')[0])
        if clean_path.startswith('/api/'):
            local_api.handle(self, 'POST', clean_path, self.db)
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        """Preflight CORS response untuk fetch browser dari Netlify HTTPS."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def do_GET(self):
        clean_path = unquote(self.path.split('?')[0])

        # 0. API Dashboard Lokal (login, log, kendaraan, buka gerbang) dari database PostgreSQL lokal
        if clean_path.startswith('/api/'):
            local_api.handle(self, 'GET', clean_path, self.db)
            return

        # 1. Endpoint Status / Health Check
        if clean_path in ('/status', '/health'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            self.wfile.write(b'{"status":"ok","camera":"active"}')
            return

        # 2. Endpoint Snapshot JPEG (Single Frame) — 100% Kebal terhadap Cloudflare Proxy Buffering
        if clean_path.startswith('/snapshot') or clean_path.startswith('/frame'):
            jpeg = GLOBAL_HUB.get_jpeg()
            if not jpeg:
                jpeg = GLOBAL_HUB.get_standby_placeholder()
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(jpeg)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
            self.wfile.write(jpeg)
            return

        # 3. Endpoint Multipart MJPEG Video Feed Stream
        if clean_path.startswith('/video_feed'):
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.send_header('Cache-Control', 'no-cache, private, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.end_headers()

            try:
                # Kirim setiap frame segera (tanpa menunggu buffer Nagle TCP)
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                last_id = -1
                while True:
                    # Tunggu frame baru dari kamera (push instan); timeout 1 dtk agar placeholder tetap terkirim
                    frame_id, jpeg = GLOBAL_HUB.wait_next(last_id, timeout=1.0)
                    if frame_id == last_id and jpeg:
                        continue
                    last_id = frame_id
                    if not jpeg:
                        jpeg = GLOBAL_HUB.get_standby_placeholder()
                    self.wfile.write(
                        b'--frame\r\nContent-Type: image/jpeg\r\n'
                        + f'Content-Length: {len(jpeg)}\r\n\r\n'.encode('utf-8')
                        + jpeg + b'\r\n'
                    )
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                pass
            return

        # 4. Layani Web Dashboard Lokal (index.html, gambar, js) langsung dari folder web_dashboard/
        self._serve_dashboard_file(clean_path)

    def _serve_dashboard_file(self, clean_path):
        """Melayani file statis Web Dashboard, Portal Registrasi Civitas (/register), & SPA routing."""
        rel_path = clean_path.lstrip('/')
        if rel_path in ('register', 'register/', 'daftar', 'daftar/'):
            rel_path = 'register.html'
        elif not rel_path or rel_path in ('dashboard', 'trendline', 'vehicle-log', 'security', 'vehicles', 'login'):
            rel_path = 'index.html'

        full_path = os.path.normpath(os.path.join(WEB_DASHBOARD_DIR, rel_path))
        if not full_path.startswith(os.path.normpath(WEB_DASHBOARD_DIR)) or not os.path.isfile(full_path):
            # Fallback SPA ke index.html
            full_path = os.path.join(WEB_DASHBOARD_DIR, 'index.html')

        if not os.path.isfile(full_path):
            self.send_response(404)
            self.end_headers()
            return

        mime_type, _ = mimetypes.guess_type(full_path)
        if not mime_type:
            mime_type = 'application/octet-stream'

        try:
            with open(full_path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', mime_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            pass

    def log_message(self, format, *args):
        return


class VideoStreamServer:
    """Server Web Dashboard Lokal + Kamera Streaming HTTP multi-thread & Cloudflare Tunnel otomatis."""
    def __init__(self, host='0.0.0.0', port=5050, enable_tunnel=True, width=640, quality=60, max_fps=0, db=None):
        self.host = host
        self.port = port
        self.enable_tunnel = enable_tunnel
        GLOBAL_HUB.configure(width=width, quality=quality, max_fps=max_fps)
        MJPEGHandler.db = db
        self.server = None
        self.server_thread = None
        self.tunnel_process = None
        self.tunnel_url = None

    def start(self):
        try:
            self.server = ThreadedHTTPServer((self.host, self.port), MJPEGHandler)
            self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.server_thread.start()
            local_ip = get_local_ip()
            print("\n" + "=" * 70)
            print("  [WEB DASHBOARD LOKAL & KAMERA LIVE (0 DELAY) SIAP DIGUNAKAN]")
            print(f"  1. Buka di Perangkat Ini      : http://localhost:{self.port}")
            print(f"  2. Buka dari Laptop/HP Lain   : http://{local_ip}:{self.port}  (1 WiFi)")
            print("=" * 70)

            # Nyalakan Cloudflare Tunnel otomatis jika diaktifkan & tersedia
            if self.enable_tunnel:
                self._start_cloudflare_tunnel()
            else:
                print("  [INFO] Cloudflare Tunnel nonaktif (CLOUDFLARE_TUNNEL_ENABLED = False): dashboard hanya lokal.")
        except Exception as e:
            print(f"[WARN] Gagal menyalakan Web Stream Server pada port {self.port}: {e}")

    def _start_cloudflare_tunnel(self):
        """Mencari cloudflared dan membuat URL HTTPS gratis untuk Netlify."""
        exe_path = None
        if os.path.isfile(CLOUDFLARED_EXE):
            exe_path = CLOUDFLARED_EXE
        elif shutil.which("cloudflared"):
            exe_path = shutil.which("cloudflared")

        if not exe_path:
            return

        def _run_tunnel():
            try:
                cmd = [exe_path, 'tunnel', '--url', f'http://localhost:{self.port}']
                self.tunnel_process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
                )

                while self.tunnel_process and self.tunnel_process.poll() is None:
                    line = self.tunnel_process.stdout.readline()
                    if not line:
                        break
                    m = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
                    if m and not self.tunnel_url:
                        self.tunnel_url = m.group(0)
                        feed_url = f"{self.tunnel_url}/video_feed"
                        print("\n" + "=" * 70)
                        print("  [CLOUDFLARE TUNNEL AKTIF - RESMI HTTPS UNTUK NETLIFY]")
                        print(f"  URL Kamera: {feed_url}")
                        print("  Salin URL di atas ke tombol [URL Cloudflare] jika buka via Netlify!")
                        print("=" * 70 + "\n")
            except Exception as e:
                print(f"[WARN] Cloudflare Tunnel mengalami kendala: {e}")

        t = threading.Thread(target=_run_tunnel, daemon=True)
        t.start()

    def update_frame(self, frame):
        GLOBAL_HUB.update_frame(frame)

    def stop(self):
        if self.tunnel_process:
            try:
                self.tunnel_process.terminate()
            except Exception:
                pass
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
