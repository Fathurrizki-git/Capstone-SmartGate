"""
Klien Plate Recognizer API untuk ALPR.
Mengirim gambar ke cloud API dan menerima hasil pembacaan plat.
"""

import io
import cv2
import numpy as np
import requests
from datetime import datetime
import config


class PlateRecognizerAPI:
    """
    Klien untuk Plate Recognizer Cloud API.
    Mengirim gambar kendaraan dan menerima hasil OCR plat nomor.
    """

    def __init__(
        self,
        api_token: str = config.API_TOKEN,
        api_url: str = config.API_URL,
        regions: list = None,
        timeout: int = config.API_TIMEOUT,
    ):
        """
        Args:
            api_token: Token API dari platerecognizer.com
            api_url: URL endpoint API
            regions: List kode region (e.g., ["id"] untuk Indonesia)
            timeout: Timeout request dalam detik
        """
        self.api_token = api_token
        self.api_url = api_url
        self.regions = regions or config.API_REGIONS
        self.timeout = timeout

        self.headers = {
            "Authorization": f"Token {self.api_token}"
        }

        # Validasi token
        if self.api_token == "YOUR_API_TOKEN_HERE":
            print("[WARNING] API Token belum diatur! Ganti di config.py")

        print(f"[OK] Plate Recognizer API client ready")
        print(f"     URL: {self.api_url}")
        print(f"     Regions: {self.regions}")

    def recognize(self, image: np.ndarray) -> dict:
        """
        Kirim gambar ke Plate Recognizer API dan terima hasil.

        Args:
            image: Gambar BGR dari OpenCV

        Returns:
            {
                "results": [
                    {
                        "plate_text": "B 1234 XYZ",
                        "confidence": 0.92,
                        "bbox": {"xmin": x1, "ymin": y1, "xmax": x2, "ymax": y2},
                        "region": {"code": "id", "score": 0.8},
                        "vehicle": {...},
                        "candidates": [...]
                    }
                ],
                "usage": {"calls": N, "max_calls": M},
                "success": True,
                "error": None
            }
        """
        try:
            # 1. Encode gambar ke JPEG bytes (hemat bandwidth)
            img_bytes = self._encode_image(image)

            # 2. Siapkan data request
            data = {}
            if self.regions:
                data["regions"] = self.regions

            files = {
                "upload": ("image.jpg", img_bytes, "image/jpeg")
            }

            # 3. Kirim ke API
            response = requests.post(
                self.api_url,
                headers=self.headers,
                files=files,
                data=data,
                timeout=self.timeout,
            )

            # 4. Parse response
            if response.status_code in [200, 201]:
                return self._parse_response(response.json())
            elif response.status_code == 403:
                return self._error_result("API Token tidak valid atau expired")
            elif response.status_code == 429:
                return self._error_result("Rate limit tercapai. Coba lagi nanti")
            else:
                return self._error_result(
                    f"API Error {response.status_code}: {response.text}"
                )

        except requests.exceptions.Timeout:
            return self._error_result("Request timeout. Periksa koneksi internet")
        except requests.exceptions.ConnectionError:
            return self._error_result("Gagal terhubung ke API. Periksa koneksi internet")
        except Exception as e:
            return self._error_result(f"Error tidak terduga: {str(e)}")

    def _encode_image(self, image: np.ndarray) -> bytes:
        """
        Encode gambar OpenCV ke JPEG bytes.
        Resize jika terlalu besar untuk menghemat bandwidth.
        """
        h, w = image.shape[:2]
        max_w, max_h = getattr(config, 'MAX_IMAGE_SIZE', (1280, 720))

        # Resize jika terlalu besar
        if w > max_w or h > max_h:
            ratio = min(max_w / w, max_h / h)
            new_size = (int(w * ratio), int(h * ratio))
            image = cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)

        # Encode ke JPEG
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, config.JPEG_QUALITY]
        _, buffer = cv2.imencode(".jpg", image, encode_params)

        return buffer.tobytes()

    def _parse_response(self, data: dict) -> dict:
        """
        Parse JSON response dari API ke format yang lebih sederhana.
        """
        results = []

        for result in data.get("results", []):
            plate_info = {
                "plate_text": result.get("plate", "").upper(),
                "confidence": result.get("score", 0.0),
                "bbox": result.get("box", {}),
                "region": result.get("region", {}),
                "vehicle": result.get("vehicle", {}),
                "candidates": [
                    {"plate": c.get("plate", "").upper(), "score": c.get("score", 0.0)}
                    for c in result.get("candidates", [])
                ],
            }
            results.append(plate_info)

        return {
            "results": results,
            "usage": data.get("usage", {}),
            "success": True,
            "error": None,
        }

    def _error_result(self, message: str) -> dict:
        """Buat result error."""
        print(f"[ERROR] {message}")
        return {
            "results": [],
            "usage": {},
            "success": False,
            "error": message,
        }

    def check_usage(self) -> dict:
        """
        Cek sisa kuota API.

        Returns:
            {"calls": int, "max_calls": int, "remaining": int}
        """
        try:
            response = requests.get(
                "https://api.platerecognizer.com/v1/statistics/",
                headers=self.headers,
                timeout=self.timeout,
            )
            if response.status_code == 200:
                data = response.json()
                usage = data.get("usage", {})
                total = data.get("total_calls", 0)
                calls = usage.get("calls", 0)
                return {
                    "calls": calls,
                    "max_calls": total,
                    "remaining": total - calls,
                }
            return {"calls": -1, "max_calls": -1, "remaining": -1}
        except Exception:
            return {"calls": -1, "max_calls": -1, "remaining": -1}


def draw_results(image: np.ndarray, results: list) -> np.ndarray:
    """
    Gambar bounding box dan teks plat pada gambar.

    Args:
        image: Gambar asli
        results: List hasil dari API

    Returns:
        Gambar dengan anotasi
    """
    annotated = image.copy()
    h_img, w_img = annotated.shape[:2]

    for r in results:
        bbox = r.get("bbox", {})
        if not bbox:
            continue

        # Koordinat bounding box
        x1 = int(bbox.get("xmin", 0))
        y1 = int(bbox.get("ymin", 0))
        x2 = int(bbox.get("xmax", 0))
        y2 = int(bbox.get("ymax", 0))

        plate_text = r.get("plate_text", "???")
        confidence = r.get("confidence", 0.0)

        # Gambar bounding box
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)

        # Label
        label = f"{plate_text} ({confidence:.0%})"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(annotated, (x1, y1 - th - 10), (x1 + tw, y1), (0, 255, 0), -1)
        cv2.putText(
            annotated, label, (x1, y1 - 5),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2
        )

    return annotated
