/*
 * ============================================================
 *  Firmware UJI LCD — Komunikasi Mini PC <-> ESP32 (USB Serial)
 *  Smart Parking Exit Gate FT UNTIRTA (Capstone A7)
 * ============================================================
 *  Khusus menguji LCD I2C (16x2 atau 20x4) lewat USB Serial 115200.
 *  TIDAK memakai sensor IR, push button, maupun servo. Pin relay buzzer
 *  (GPIO26) hanya dikunci ke posisi MATI agar buzzer tidak berbunyi.
 *
 *  Board  : ESP32 Dev Module
 *  Library: LiquidCrystal_I2C (sama dengan firmware gerbang)
 *  Pin    : LCD SDA GPIO21, SCL GPIO22 (alamat dicari otomatis, umumnya 0x27 / 0x3F)
 *
 *  Protokol (teks per baris '\n', kompatibel dengan firmware gerbang untuk PING/LCD/LCDCLR):
 *   Mini PC -> ESP32 : PING                      -> PONG
 *                      INFO                      -> INFO:<nama>:<kolom>x<baris>:<alamat|NONE>
 *                      SIZE:<kolom>x<baris>      -> OK:SIZE:<kolom>x<baris>  (16x2 atau 20x4) | ERR:SIZE
 *                      LCD:<b1>|<b2>|<b3>|<b4>   -> OK:LCD:<waktu tulis I2C ms>  | ERR:LCD_NOT_FOUND
 *                      LCDCLR                    -> OK:LCDCLR
 *   ESP32 -> Mini PC : READY:UJI_LCD_ESP32_V1 (saat boot), READY:LCD_OK_0x27 / ERR:LCD_NOT_FOUND,
 *                      ERR:UNKNOWN:<perintah>
 *  Jika Mini PC diam > 5 detik, LCD menampilkan "MINI PC OFFLINE!" (sama dengan firmware gerbang).
 *
 *  Setelah selesai menguji, flash kembali firmware/smart_gate_esp32/smart_gate_esp32.ino.
 * ============================================================
 */

#include <Wire.h>
#include <LiquidCrystal_I2C.h>

#define PIN_RELAY  26
#define PIN_SDA    21
#define PIN_SCL    22

const char    *FW_NAME          = "UJI_LCD_ESP32_V1";
const uint32_t SERIAL_BAUD      = 115200;   // Harus sama dengan ESP32_BAUD di config.py
const uint32_t PC_TIMEOUT_MS    = 5000;     // Tampilkan peringatan jika Mini PC diam selama ini
const bool     RELAY_ACTIVE_LOW = true;     // Sama dengan firmware gerbang
const uint8_t  RELAY_OFF        = RELAY_ACTIVE_LOW ? HIGH : LOW;
const uint8_t  MAX_ROWS         = 4;

LiquidCrystal_I2C *lcd = nullptr;
uint8_t  lcdAddr  = 0;
uint8_t  lcdCols  = 16;          // Ukuran awal 16x2; Mini PC mengirim SIZE:20x4 untuk LCD 20x4
uint8_t  lcdRows  = 2;
String   lcdLines[MAX_ROWS];
bool     lcdOverlay = false;     // true saat layar menampilkan peringatan "Mini PC offline"

String   rxBuffer;
uint32_t lastPcMs = 0;
bool     pcOnline = false;

// ── LCD ──────────────────────────────────────────
uint8_t scanI2cLcdAddress() {
  const uint8_t addrs[] = {0x27, 0x3F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26,
                           0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E};
  for (uint8_t a : addrs) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) return a;
  }
  return 0;
}

void recoverI2cBus() {
  // Lepaskan SDA yang tertahan LOW oleh PCF8574 bila ESP32 reset di tengah transaksi I2C
  pinMode(PIN_SDA, INPUT_PULLUP);
  pinMode(PIN_SCL, OUTPUT);
  for (uint8_t i = 0; i < 9; i++) {
    digitalWrite(PIN_SCL, LOW);  delayMicroseconds(10);
    digitalWrite(PIN_SCL, HIGH); delayMicroseconds(10);
  }
  pinMode(PIN_SDA, OUTPUT);
  digitalWrite(PIN_SDA, LOW);  delayMicroseconds(10);
  digitalWrite(PIN_SDA, HIGH); delayMicroseconds(10);
}

String padLine(const String &s) {
  String out = s.substring(0, lcdCols);
  while (out.length() < lcdCols) out += ' ';
  return out;
}

void lcdWriteRow(uint8_t row, const String &text) {
  if (!lcd || row >= lcdRows) return;
  lcd->setCursor(0, row);
  lcd->print(padLine(text));
}

void lcdRedrawAll() {
  for (uint8_t r = 0; r < lcdRows; r++) lcdWriteRow(r, lcdLines[r]);
}

// Buat ulang objek LCD dengan ukuran sekarang (dipanggil saat boot dan saat SIZE:)
bool initLcd() {
  if (lcd) { delete lcd; lcd = nullptr; }
  if (!lcdAddr) lcdAddr = scanI2cLcdAddress();
  if (!lcdAddr) return false;
  lcd = new LiquidCrystal_I2C(lcdAddr, lcdCols, lcdRows);
  lcd->begin();
  Wire.setTimeOut(20);
  lcd->backlight();
  lcd->clear();
  return true;
}

void lcdSetLines(const String &payload) {
  int start = 0;
  for (uint8_t r = 0; r < lcdRows; r++) {
    int sep = payload.indexOf('|', start);
    String part = (sep < 0) ? payload.substring(start) : payload.substring(start, sep);
    start = (sep < 0) ? payload.length() : sep + 1;
    lcdLines[r] = padLine(part);
    lcdWriteRow(r, lcdLines[r]);
  }
  lcdOverlay = false;
}

// ── Perintah Mini PC ─────────────────────────────
void handleCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  if (cmd == "PING") {
    Serial.println("PONG");
  } else if (cmd == "INFO") {
    Serial.printf("INFO:%s:%dx%d:", FW_NAME, lcdCols, lcdRows);
    if (lcd) Serial.printf("0x%02X\n", lcdAddr); else Serial.println("NONE");
  } else if (cmd.startsWith("SIZE:")) {
    int x = cmd.indexOf('x', 5);
    int c = x < 0 ? 0 : cmd.substring(5, x).toInt();
    int r = x < 0 ? 0 : cmd.substring(x + 1).toInt();
    if (!((c == 16 && r == 2) || (c == 20 && r == 4))) {
      Serial.println("ERR:SIZE");
      return;
    }
    lcdCols = c; lcdRows = r;
    for (uint8_t i = 0; i < MAX_ROWS; i++) lcdLines[i] = padLine("");
    if (initLcd()) Serial.printf("OK:SIZE:%dx%d\n", lcdCols, lcdRows);
    else Serial.println("ERR:LCD_NOT_FOUND");
  } else if (cmd.startsWith("LCD:")) {
    if (!lcd) { Serial.println("ERR:LCD_NOT_FOUND"); return; }
    uint32_t t0 = millis();
    lcdSetLines(cmd.substring(4));
    Serial.printf("OK:LCD:%lu\n", (unsigned long)(millis() - t0));
  } else if (cmd == "LCDCLR") {
    for (uint8_t r = 0; r < MAX_ROWS; r++) lcdLines[r] = padLine("");
    if (lcd) lcd->clear();
    lcdOverlay = false;
    Serial.println("OK:LCDCLR");
  } else {
    Serial.print("ERR:UNKNOWN:");
    Serial.println(cmd);
  }
}

void readSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    lastPcMs = millis();
    if (!pcOnline) {
      pcOnline = true;
      if (lcdOverlay) { lcdOverlay = false; lcdRedrawAll(); }
    }
    if (c == '\n') {
      handleCommand(rxBuffer);
      rxBuffer = "";
    } else if (c != '\r') {
      if (rxBuffer.length() < 200) rxBuffer += c;
    }
  }
}

void checkPcTimeout() {
  if (pcOnline && millis() - lastPcMs > PC_TIMEOUT_MS) {
    pcOnline = false;
    lcdOverlay = true;
    lcdWriteRow(0, "MINI PC OFFLINE!");
    lcdWriteRow(1, "UJI WATCHDOG OK");
    if (lcdRows >= 4) {
      lcdWriteRow(2, "TIDAK ADA DATA");
      lcdWriteRow(3, "SELAMA 5 DETIK");
    }
  }
}

// ── Setup & Loop ─────────────────────────────────
void setup() {
  Serial.begin(SERIAL_BAUD);

  // Kunci relay buzzer ke posisi MATI (set level dulu sebelum OUTPUT agar tidak sempat berkontak)
  digitalWrite(PIN_RELAY, RELAY_OFF);
  pinMode(PIN_RELAY, OUTPUT);
  digitalWrite(PIN_RELAY, RELAY_OFF);

  delay(100);  // Beri waktu catu daya modul I2C LCD stabil
  recoverI2cBus();
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setTimeOut(20);

  Serial.printf("READY:%s\n", FW_NAME);
  if (initLcd()) {
    lcdLines[0] = padLine("UJI LCD ESP32");
    lcdLines[1] = padLine("TUNGGU MINI PC..");
    lcdRedrawAll();
    Serial.printf("READY:LCD_OK_0x%02X\n", lcdAddr);
  } else {
    Serial.println("ERR:LCD_NOT_FOUND");
  }
}

void loop() {
  readSerial();
  checkPcTimeout();
}
