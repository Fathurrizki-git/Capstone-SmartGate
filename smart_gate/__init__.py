"""
Paket program Smart Parking Exit Gate FT UNTIRTA.

  hardware/  Perangkat keras gerbang (ESP32: LCD, servo, buzzer, sensor IR, push button) & RFID USB
  vision/    Kamera webcam & pengenalan plat nomor (Plate Recognizer API)
  database/  Database lokal PostgreSQL, verifikasi plat + RFID, sinkronisasi Supabase
  web/       Server dashboard lokal, live kamera, & API dashboard
"""

import os

# Folder utama proyek (tempat main.py, config.py, web_dashboard/, cloudflared.exe)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
