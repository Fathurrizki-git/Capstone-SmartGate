"""
ALPR lokal (offline) di Mini PC: YOLOv8n (deteksi plat) + PP-OCRv5 rec (baca teks), keduanya format ONNX.

Hanya butuh onnxruntime + OpenCV + numpy (tanpa PyTorch / PaddlePaddle), sehingga jalan di
Python versi baru (PaddlePaddle belum punya wheel untuk Python 3.14).

File model dibuat di notebook Colab alpr_model/new/alpr.ipynb (bagian "Ekspor model untuk Mini PC")
lalu disalin ke folder config.ALPR_MODEL_DIR:
    plate_det.onnx       YOLOv8n hasil fine-tune (1 kelas: plate), input 640x640
    plate_rec.onnx       PaddleOCR en_PP-OCRv5_mobile_rec
    plate_rec_dict.txt   daftar karakter model OCR (satu karakter per baris)

recognize() mengembalikan format yang SAMA dengan PlateRecognizerAPI.recognize() agar main.py tidak berubah.
Logika teks plat (clean / fix_plate / crop_main_line) harus sama dengan plate_utils.py di notebook.
"""

import math
import os
import re
import time

import cv2
import numpy as np

import config
import smart_gate


# ── Teks plat Indonesia (salinan plate_utils.py di notebook) ─────────────
LETTER_FIX = {'0': 'O', '1': 'I', '2': 'Z', '4': 'A', '5': 'S', '6': 'G', '7': 'T', '8': 'B'}
DIGIT_FIX = {'O': '0', 'D': '0', 'Q': '0', 'U': '0', 'I': '1', 'L': '1', 'J': '1',
             'Z': '2', 'A': '4', 'S': '5', 'G': '6', 'T': '7', 'B': '8'}
PATTERN = re.compile(r'^([A-Z]{1,2})([0-9]{1,4})([A-Z]{0,3})$')
REGION_CODES = set('A B D E F G H K L M N P R S T W Z'.split()) | set(
    'AA AB AD AE AG BA BB BD BE BG BH BK BL BM BN BP DA DB DC DD DE DG DH DK DL DM DN DR DS DT '
    'EA EB ED KB KH KT KU PA PB'.split())
SLASHED_O = str.maketrans({'Ø': 'O', 'ø': 'O'})


def clean(text):
    """Huruf kapital, O bergaris miring (Ø) -> huruf O, buang semua selain A-Z dan 0-9."""
    return re.sub(r'[^A-Z0-9]', '', str(text).translate(SLASHED_O).upper())


def fix_plate(text):
    """Cari pembagian [huruf 1-2][angka 1-4][huruf 0-3] dengan koreksi karakter paling sedikit.
    Mengembalikan string tanpa spasi (mis. 'B1234XYZ') atau None bila tidak bisa dibentuk."""
    s = clean(text)
    if len(s) > 9 and s[-4:].isdigit():        # sisa baris masa berlaku (MM.YY) ikut terbaca
        s = s[:-4]
    best = None
    for a in (1, 2):
        for b in range(1, 5):
            c = len(s) - a - b
            if c < 0 or c > 3:
                continue
            out, cost, ok = [], 0, True
            for i, ch in enumerate(s):
                if i < a or i >= a + b:          # posisi huruf
                    if ch.isalpha():
                        out.append(ch)
                    elif ch in LETTER_FIX:
                        out.append(LETTER_FIX[ch]); cost += 1
                    else:
                        ok = False; break
                else:                             # posisi angka
                    if ch.isdigit():
                        out.append(ch)
                    elif ch in DIGIT_FIX:
                        out.append(DIGIT_FIX[ch]); cost += 1
                    else:
                        ok = False; break
            if ok:
                if ''.join(out[:a]) not in REGION_CODES:
                    cost += 2                     # kode wilayah tidak dikenal
                key = (cost, -b)
                if best is None or key < best[0]:
                    best = (key, ''.join(out))
    return best[1] if best else None


def format_plate(plate):
    """'B1234XYZ' -> 'B 1234 XYZ'"""
    m = PATTERN.match(plate or '')
    return ' '.join(g for g in m.groups() if g) if m else (plate or '')


def crop_main_line(crop):
    """Ambil baris nomor utama; buang baris masa berlaku (MM.YY) HANYA bila terpisah jelas."""
    h, w = crop.shape[:2]
    if h < 16 or w < 16:
        return crop
    x0, x1 = int(w * 0.08), int(w * 0.92)
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    g = g[:, x0:x1]
    _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if b.mean() > 127:                       # plat putih teks hitam -> balik agar teks = putih
        b = 255 - b
    prof = (b > 0).mean(axis=1)
    prof[prof > 0.80] = 0                    # garis bingkai plat (hampir penuh) bukan teks
    on = prof > max(0.04, prof.max() * 0.15)
    bands, start = [], None
    for y, v in enumerate(np.append(on, False)):
        if v and start is None:
            start = y
        elif not v and start is not None:
            if y - start >= 2:
                bands.append((start, y))
            start = None
    if len(bands) < 2:
        return crop
    main = max(bands, key=lambda t: t[1] - t[0])
    mh = main[1] - main[0]
    below = [t for t in bands if t[0] >= main[1]]
    if mh < h * 0.22 or not below:
        return crop
    date = below[0]
    if (date[1] - date[0]) > mh * 0.75:      # pita di bawah sama besar -> bukan baris tanggal
        return crop
    cut = (main[1] + date[0]) // 2
    top = max(0, main[0] - int(mh * 0.25))
    return crop[top:cut]


def crop_plate(img, x1, y1, x2, y2, mx=0.04, my=0.10):
    """Potong plat dengan sedikit margin (sama seperti notebook)."""
    h, w = img.shape[:2]
    bw, bh = x2 - x1, y2 - y1
    return img[int(max(0, y1 - bh * my)):int(min(h, y2 + bh * my)),
               int(max(0, x1 - bw * mx)):int(min(w, x2 + bw * mx))]


# ── Model ONNX ───────────────────────────────────────────────────────────
class PlateDetector:
    """YOLOv8n ONNX (keluaran [1, 4+1, N]: cx, cy, w, h, skor kelas plate)."""

    def __init__(self, sess, conf=0.5, iou=0.45):
        self.sess = sess
        self.input_name = sess.get_inputs()[0].name
        shape = sess.get_inputs()[0].shape            # [1, 3, 640, 640]
        self.size = int(shape[2]) if isinstance(shape[2], int) else 640
        self.conf, self.iou = conf, iou

    def detect(self, img):
        h, w = img.shape[:2]
        r = min(self.size / h, self.size / w)
        nw, nh = int(round(w * r)), int(round(h * r))
        px, py = (self.size - nw) // 2, (self.size - nh) // 2
        canvas = np.full((self.size, self.size, 3), 114, np.uint8)       # letterbox seperti Ultralytics
        canvas[py:py + nh, px:px + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0   # BGR->RGB, NCHW

        pred = self.sess.run(None, {self.input_name: blob})[0][0]       # [5, N]
        pred = pred.T if pred.shape[0] < pred.shape[1] else pred         # [N, 5]
        scores = pred[:, 4:].max(axis=1)
        keep = scores >= self.conf
        boxes, scores = pred[keep, :4], scores[keep]
        if not len(boxes):
            return []
        xywh = np.stack([(boxes[:, 0] - boxes[:, 2] / 2 - px) / r, (boxes[:, 1] - boxes[:, 3] / 2 - py) / r,
                         boxes[:, 2] / r, boxes[:, 3] / r], axis=1)
        idx = cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), self.conf, self.iou)
        out = []
        for i in np.array(idx).flatten():
            x, y, bw, bh = xywh[i]
            out.append((max(0.0, x), max(0.0, y), min(float(w), x + bw), min(float(h), y + bh), float(scores[i])))
        return sorted(out, key=lambda t: -t[4])


class PlateReader:
    """PP-OCRv5 rec ONNX + decode CTC (prapemrosesan sama dengan PaddleOCR: tinggi 48, normalisasi [-1, 1])."""

    def __init__(self, sess, chars, img_h=48, img_w=320):
        self.sess = sess
        self.input_name = sess.get_inputs()[0].name
        self.chars = chars
        self.img_h, self.img_w = img_h, img_w

    def read(self, crop):
        h, w = crop.shape[:2]
        img_w = max(self.img_w, int(self.img_h * w / max(h, 1)))
        rw = min(img_w, int(math.ceil(self.img_h * w / max(h, 1))))
        x = cv2.resize(crop, (max(rw, 1), self.img_h)).astype(np.float32)
        x = (x.transpose(2, 0, 1) / 255.0 - 0.5) / 0.5
        blob = np.zeros((1, 3, self.img_h, img_w), np.float32)
        blob[0, :, :, :x.shape[2]] = x

        prob = self.sess.run(None, {self.input_name: blob})[0][0]       # [T, kelas]
        if len(self.chars) < prob.shape[1]:                               # karakter spasi di akhir
            self.chars = self.chars + [' '] * (prob.shape[1] - len(self.chars))
        idx, p = prob.argmax(axis=1), prob.max(axis=1)
        text, conf, prev = [], [], -1
        for i, pi in zip(idx, p):
            if i != prev and i != 0:                                      # 0 = blank CTC
                text.append(self.chars[i]); conf.append(pi)
            prev = i
        return ''.join(text), float(np.mean(conf)) if conf else 0.0


class LocalALPR:
    """Pengganti PlateRecognizerAPI yang berjalan offline di Mini PC."""

    def __init__(self, model_dir: str = None, det_conf: float = None):
        import onnxruntime as ort

        model_dir = model_dir or getattr(config, 'ALPR_MODEL_DIR', 'alpr_model/weights')
        if not os.path.isabs(model_dir):
            model_dir = os.path.join(smart_gate.PROJECT_ROOT, model_dir)
        det_path = os.path.join(model_dir, 'plate_det.onnx')
        rec_path = os.path.join(model_dir, 'plate_rec.onnx')
        dict_path = os.path.join(model_dir, 'plate_rec_dict.txt')
        for p in (det_path, rec_path, dict_path):
            if not os.path.exists(p):
                raise FileNotFoundError(f"File model ALPR tidak ditemukan: {p}")

        with open(dict_path, encoding='utf-8') as f:
            chars = ['blank'] + [ln.rstrip('\r\n') for ln in f if ln.rstrip('\r\n')]

        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        providers = ['CPUExecutionProvider']
        self.detector = PlateDetector(ort.InferenceSession(det_path, opts, providers=providers),
                                      conf=det_conf if det_conf is not None else getattr(config, 'ALPR_DET_CONF', 0.5))
        self.reader = PlateReader(ort.InferenceSession(rec_path, opts, providers=providers), chars)

        # Pemanasan agar pindaian pertama tidak lambat
        self.recognize(np.zeros((480, 640, 3), np.uint8))
        print(f"[OK] ALPR lokal siap (YOLOv8n + PP-OCRv5, ONNX CPU)")
        print(f"     Model: {model_dir}")

    def recognize(self, image: np.ndarray) -> dict:
        """Format keluaran sama dengan PlateRecognizerAPI.recognize(). Plat yang sesuai pola diurutkan lebih dulu."""
        try:
            t0 = time.time()
            results = []
            for x1, y1, x2, y2, det_conf in self.detector.detect(image):
                crop = crop_main_line(crop_plate(image, x1, y1, x2, y2))
                if crop.size == 0:
                    continue
                raw, ocr_conf = self.reader.read(crop)
                plate = fix_plate(raw)
                results.append({
                    "plate_text": format_plate(plate) if plate else clean(raw),
                    "confidence": round(ocr_conf, 3),
                    "bbox": {"xmin": int(x1), "ymin": int(y1), "xmax": int(x2), "ymax": int(y2)},
                    "region": {"code": "id", "score": det_conf},
                    "vehicle": {},
                    "candidates": [{"plate": clean(raw), "score": round(ocr_conf, 3)}],
                    "valid": bool(plate),
                    "raw_text": raw,
                })
            results = [r for r in results if r["plate_text"]]
            results.sort(key=lambda r: (not r["valid"], -r["confidence"] * r["region"]["score"]))
            return {"results": results, "usage": {"local_ms": int((time.time() - t0) * 1000)},
                    "success": True, "error": None}
        except Exception as e:
            print(f"[ERROR] ALPR lokal gagal: {e}")
            return {"results": [], "usage": {}, "success": False, "error": f"ALPR lokal gagal: {e}"}


def create_alpr():
    """Pilih mesin ALPR sesuai config.ALPR_ENGINE ("LOCAL" / "CLOUD").
    LOCAL yang gagal dimuat (model/onnxruntime belum ada) otomatis jatuh ke Plate Recognizer cloud."""
    engine = str(getattr(config, 'ALPR_ENGINE', 'CLOUD')).upper()
    if engine == 'LOCAL':
        try:
            return LocalALPR()
        except Exception as e:
            print(f"[WARN] ALPR lokal tidak bisa dimuat ({e}). Memakai Plate Recognizer cloud.")
    from smart_gate.vision.plate_recognizer import PlateRecognizerAPI
    return PlateRecognizerAPI()
