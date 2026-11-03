/*
 * ============================================================
 * Firmware ESP32 — Smart Parking Exit Gate FT UNTIRTA
 * Kontroler I/O gerbang yang dikendalikan Mini PC via USB Serial.
 * ============================================================
 *
 * Board   : ESP32 Dev Module (ESP32-WROOM-32 DevKit V1)
 * Library : - ESP32Servo         (Kevin Harrington / madhephaestus)
 *           - LiquidCrystal_I2C  (Frank de Brabander)
 *
 * Komponen yang terhubung ke ESP32:
 *   Sensor IR 1 E18-D80NK (Sebelum Palang) -> GPIO32 (Active LOW, INPUT_PULLUP)
 *   Sensor IR 2 E18-D80NK (Setelah Palang) -> GPIO33 (Active LOW, INPUT_PULLUP)
 *   Push Button Manual                     -> GPIO27 (Active LOW, INPUT_PULLUP)
 *   Motor Servo Palang (Signal)            -> GPIO13 (PWM 50Hz)
 *   Buzzer Flash 12V (Basis Transistor NPN)-> GPIO26 (Active HIGH)
 *   LCD I2C 20x4                           -> SDA GPIO21, SCL GPIO22
 *
 * Protokol (teks per baris, 115200 baud) — lihat juga esp32_link.py:
 *   Mini PC -> ESP32 : PING | STATUS | SERVO:<deg> | BEEP:<ms>:<n> | ALERT:<ms>
 *                      LCD:<b1>|<b2>|<b3>|<b4> | LCDCLR
 *   ESP32 -> Mini PC : READY:<nama> | PONG | ST:<ir1>:<ir2>:<btn>
 *                      EVT:IR1:<0|1> | EVT:IR2:<0|1> | EVT:BTN:<0|1> | ERR:<pesan>
 */

#include <Arduino.h>
#include <Wire.h>
#include <ESP32Servo.h>
#include <LiquidCrystal_I2C.h>

// ── Pemetaan Pin ─────────────────────────────────
#define PIN_IR_BEFORE   32
#define PIN_IR_AFTER    33
#define PIN_BUTTON      27
#define PIN_SERVO       13
#define PIN_BUZZER      26
#define PIN_SDA         21
#define PIN_SCL         22

// ── Parameter ────────────────────────────────────
const uint32_t SERIAL_BAUD        = 115200;  // Harus sama dengan ESP32_BAUD di config.py
const uint8_t  LCD_COLS           = 20;
const uint8_t  LCD_ROWS           = 4;
const uint16_t SERVO_MIN_US       = 500;     // Pulsa 0 derajat  (2.5% duty @50Hz)
const uint16_t SERVO_MAX_US       = 2500;    // Pulsa 180 derajat (12.5% duty @50Hz)
const uint32_t SERVO_SETTLE_MS    = 500;     // Waktu servo mencapai posisi sebelum sinyal dilepas
const bool     SERVO_DETACH_IDLE  = true;    // Lepas sinyal setelah bergerak agar tidak jitter / panas
const uint32_t IR_DEBOUNCE_MS     = 30;
const uint32_t BUTTON_DEBOUNCE_MS = 50;
const uint32_t HEARTBEAT_MS       = 250;     // Interval kirim status ST:...
const uint32_t PC_TIMEOUT_MS      = 5000;    // Tampilkan peringatan LCD jika Mini PC diam selama ini
const uint16_t BEEP_GAP_MS        = 80;

// ── Input dengan Debounce ────────────────────────
struct DebouncedInput {
  uint8_t  pin;
  uint32_t debounceMs;
  bool     stable;     // true = aktif (LOW)
  bool     lastRaw;
  uint32_t changedAt;
};

DebouncedInput irBefore = {PIN_IR_BEFORE, IR_DEBOUNCE_MS, false, false, 0};
DebouncedInput irAfter  = {PIN_IR_AFTER,  IR_DEBOUNCE_MS, false, false, 0};
DebouncedInput button   = {PIN_BUTTON,    BUTTON_DEBOUNCE_MS, false, false, 0};

// Return true jika status stabil berubah
bool updateInput(DebouncedInput &in) {
  bool raw = digitalRead(in.pin) == LOW;
  uint32_t now = millis();
  if (raw != in.lastRaw) {
    in.lastRaw = raw;
    in.changedAt = now;
  }
  if (raw != in.stable && now - in.changedAt >= in.debounceMs) {
    in.stable = raw;
    return true;
  }
  return false;
}

// ── Buzzer Non-Blocking ──────────────────────────
struct BuzzerPattern {
  uint16_t onMs;
  uint16_t offMs;
  uint8_t  remaining;
  bool     on;
  uint32_t nextAt;
} buzzer = {0, 0, 0, false, 0};

void startBuzzer(uint16_t onMs, uint16_t offMs, uint8_t count) {
  digitalWrite(PIN_BUZZER, LOW);
  buzzer = {onMs, offMs, count, false, millis()};
}

void updateBuzzer() {
  if (!buzzer.on && buzzer.remaining == 0) return;
  uint32_t now = millis();
  if ((int32_t)(now - buzzer.nextAt) < 0) return;

  if (buzzer.on) {
    digitalWrite(PIN_BUZZER, LOW);
    buzzer.on = false;
    buzzer.remaining--;
    buzzer.nextAt = now + buzzer.offMs;
  } else if (buzzer.remaining > 0) {
    digitalWrite(PIN_BUZZER, HIGH);
    buzzer.on = true;
    buzzer.nextAt = now + buzzer.onMs;
  }
}

// ── Servo ────────────────────────────────────────
Servo gateServo;
bool     servoAttached = false;
uint32_t servoDetachAt = 0;

void moveServo(int angle) {
  angle = constrain(angle, 0, 180);
  if (!servoAttached) {
    gateServo.attach(PIN_SERVO, SERVO_MIN_US, SERVO_MAX_US);
    servoAttached = true;
  }
  gateServo.write(angle);
  servoDetachAt = millis() + SERVO_SETTLE_MS;
}

void updateServo() {
  if (SERVO_DETACH_IDLE && servoAttached && (int32_t)(millis() - servoDetachAt) >= 0) {
    gateServo.detach();
    servoAttached = false;
  }
}

// ── LCD ──────────────────────────────────────────
LiquidCrystal_I2C *lcd = nullptr;
String lcdLines[LCD_ROWS];   // Isi terakhir yang diminta Mini PC
bool   lcdOverlay = false;   // true saat layar menampilkan peringatan "Mini PC offline"

uint8_t detectLcdAddress() {
  const uint8_t candidates[] = {0x27, 0x3F};
  for (uint8_t addr : candidates) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) return addr;
  }
  return 0;
}

String padLine(const String &s) {
  String out = s.substring(0, LCD_COLS);
  while (out.length() < LCD_COLS) out += ' ';
  return out;
}

void lcdWriteRow(uint8_t row, const String &text) {
  if (!lcd) return;
  lcd->setCursor(0, row);
  lcd->print(padLine(text));
}

void lcdRedrawAll() {
  for (uint8_t r = 0; r < LCD_ROWS; r++) lcdWriteRow(r, lcdLines[r]);
}

void lcdSetLines(const String &payload) {
  int start = 0;
  for (uint8_t r = 0; r < LCD_ROWS; r++) {
    int sep = payload.indexOf('|', start);
    String part = (sep < 0) ? payload.substring(start) : payload.substring(start, sep);
    start = (sep < 0) ? payload.length() : sep + 1;
    part = padLine(part);
    // Tulis hanya baris yang berubah agar LCD tidak berkedip
    if (part != lcdLines[r] || lcdOverlay) {
      lcdLines[r] = part;
      lcdWriteRow(r, part);
    }
  }
  lcdOverlay = false;
}

// ── Komunikasi Mini PC ───────────────────────────
String   rxBuffer;
uint32_t lastPcMs = 0;
bool     pcOnline = false;
uint32_t lastHeartbeat = 0;

void sendStatus() {
  Serial.printf("ST:%d:%d:%d\n", irBefore.stable, irAfter.stable, button.stable);
}

void handleCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  if (cmd == "PING") {
    Serial.println("PONG");
  } else if (cmd == "STATUS") {
    sendStatus();
  } else if (cmd.startsWith("SERVO:")) {
    moveServo(cmd.substring(6).toInt());
  } else if (cmd.startsWith("BEEP:")) {
    int sep = cmd.indexOf(':', 5);
    uint16_t onMs = cmd.substring(5, sep < 0 ? cmd.length() : sep).toInt();
    uint8_t count = sep < 0 ? 1 : cmd.substring(sep + 1).toInt();
    startBuzzer(onMs, BEEP_GAP_MS, count < 1 ? 1 : count);
  } else if (cmd.startsWith("ALERT:")) {
    startBuzzer(cmd.substring(6).toInt(), 0, 1);
  } else if (cmd.startsWith("LCD:")) {
    lcdSetLines(cmd.substring(4));
  } else if (cmd == "LCDCLR") {
    for (uint8_t r = 0; r < LCD_ROWS; r++) lcdLines[r] = padLine("");
    if (lcd) lcd->clear();
    lcdOverlay = false;
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
      if (lcdOverlay) {
        lcdOverlay = false;
        lcdRedrawAll();  // Kembalikan tampilan terakhir dari Mini PC
      }
    }
    if (c == '\n') {
      handleCommand(rxBuffer);
      rxBuffer = "";
    } else if (c != '\r') {
      if (rxBuffer.length() < 160) rxBuffer += c;
    }
  }
}

void checkPcTimeout() {
  if (pcOnline && millis() - lastPcMs > PC_TIMEOUT_MS) {
    pcOnline = false;
    lcdOverlay = true;
    lcdWriteRow(0, " GERBANG KELUAR FT  ");
    lcdWriteRow(1, "! MINI PC OFFLINE ! ");
    lcdWriteRow(2, "HUBUNGI PETUGAS     ");
    lcdWriteRow(3, "PALANG TIDAK BERUBAH");
  }
}

// ── Setup & Loop ─────────────────────────────────
void setup() {
  Serial.begin(SERIAL_BAUD);

  pinMode(PIN_IR_BEFORE, INPUT_PULLUP);
  pinMode(PIN_IR_AFTER, INPUT_PULLUP);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);

  // Servo tidak digerakkan saat boot: jika ESP32 reset ketika kendaraan
  // berada di bawah palang, posisi palang dibiarkan hingga Mini PC memberi perintah.
  ESP32PWM::allocateTimer(0);
  gateServo.setPeriodHertz(50);

  Wire.begin(PIN_SDA, PIN_SCL);
  uint8_t lcdAddr = detectLcdAddress();
  if (lcdAddr) {
    lcd = new LiquidCrystal_I2C(lcdAddr, LCD_COLS, LCD_ROWS);
    lcd->init();
    lcd->backlight();
  }
  lcdLines[0] = padLine(" GERBANG KELUAR FT  ");
  lcdLines[1] = padLine("  UNTIRTA CILEGON   ");
  lcdLines[2] = padLine("MENUNGGU MINI PC... ");
  lcdLines[3] = padLine("");
  lcdRedrawAll();

  irBefore.stable = irBefore.lastRaw = digitalRead(PIN_IR_BEFORE) == LOW;
  irAfter.stable  = irAfter.lastRaw  = digitalRead(PIN_IR_AFTER) == LOW;
  button.stable   = button.lastRaw   = digitalRead(PIN_BUTTON) == LOW;

  Serial.println("READY:SMART_GATE_ESP32_V1");
  if (!lcd) Serial.println("ERR:LCD_NOT_FOUND");
  sendStatus();
}

void loop() {
  readSerial();

  if (updateInput(irBefore)) Serial.printf("EVT:IR1:%d\n", irBefore.stable);
  if (updateInput(irAfter))  Serial.printf("EVT:IR2:%d\n", irAfter.stable);
  if (updateInput(button))   Serial.printf("EVT:BTN:%d\n", button.stable);

  updateBuzzer();
  updateServo();
  checkPcTimeout();

  if (millis() - lastHeartbeat >= HEARTBEAT_MS) {
    lastHeartbeat = millis();
    sendStatus();
  }
}
