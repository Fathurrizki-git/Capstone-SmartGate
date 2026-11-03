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

WEB_DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_dashboard")


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
        self.lock = threading.Lock()
        self.latest_jpeg = None
        self.last_update = 0.0
        self._placeholder_jpeg = None

    def update_frame(self, frame_bgr):
        if frame_bgr is None:
            return
        try:
            # Optimasi Latensi: Resize ke 640px & JPEG Quality 60 agar ukuran frame ~10-12KB
            h, w = frame_bgr.shape[:2]
            target_w = 640
            target_h = int(h * (target_w / w))
            stream_frame = cv2.resize(frame_bgr, (target_w, target_h), interpolation=cv2.INTER_AREA)

            ret, buf = cv2.imencode(".jpg", stream_frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
            if ret:
                with self.lock:
                    self.latest_jpeg = buf.tobytes()
                    self.last_update = time.time()
        except Exception:
            pass

    def get_jpeg(self):
        with self.lock:
            return self.latest_jpeg

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
    """Handler HTTP untuk menyiarkan Web Dashboard Lokal, snapshot JPEG, multipart MJPEG stream, dan status."""

    def do_OPTIONS(self):
        """Preflight CORS response untuk fetch browser dari Netlify HTTPS."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def do_GET(self):
        clean_path = unquote(self.path.split('?')[0])

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
                last_sent = 0.0
                while True:
                    jpeg = GLOBAL_HUB.get_jpeg()
                    if not jpeg:
                        jpeg = GLOBAL_HUB.get_standby_placeholder()
                    if jpeg and GLOBAL_HUB.last_update != last_sent:
                        last_sent = GLOBAL_HUB.last_update
                        self.wfile.write(b'--frame\r\n')
                        self.wfile.write(b'Content-Type: image/jpeg\r\n')
                        self.wfile.write(f'Content-Length: {len(jpeg)}\r\n\r\n'.encode('utf-8'))
                        self.wfile.write(jpeg)
                        self.wfile.write(b'\r\n')
                    time.sleep(0.04)  # ~25 FPS
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
    def __init__(self, host='0.0.0.0', port=5050):
        self.host = host
        self.port = port
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

            # Nyalakan Cloudflare Tunnel otomatis jika tersedia
            self._start_cloudflare_tunnel()
        except Exception as e:
            print(f"[WARN] Gagal menyalakan Web Stream Server pada port {self.port}: {e}")

    def _start_cloudflare_tunnel(self):
        """Mencari cloudflared dan membuat URL HTTPS gratis untuk Netlify."""
        exe_path = None
        if os.path.isfile("cloudflared.exe"):
            exe_path = os.path.abspath("cloudflared.exe")
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
