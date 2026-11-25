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
 *   Push Button Manual Schneider XB7NA42   -> GPIO27 (Kontak NC, INPUT_PULLUP)
 *       Wiring: terminal 1 -> GPIO27, terminal 2 -> GND.
 *       Dilepas = LOW (kontak tertutup), ditekan = HIGH. Dibalik di firmware
 *       (BUTTON_IS_NC) sehingga protokol tetap: 1 = ditekan.
 *       Kabel putus/lepas terbaca "ditekan" terus: jika ditekan lebih dari
 *       BUTTON_FAULT_MS, firmware mengirim ERR:BTN_FAULT sekali dan menahan
 *       EVT:BTN:1 sampai tombol terbaca lepas lagi (lalu kirim EVT:BTN_OK).
 *       Jika saat boot sudah terbaca ditekan, tekanan itu diabaikan.
 *   Motor Servo Palang (Signal)            -> GPIO13 (PWM 50Hz)
 *   Relay Buzzer Flash 12V (Kontak NO)     -> GPIO26 (Active LOW)
 *   LCD I2C 20x4                           -> SDA GPIO21, SCL GPIO22
 *
 * Protokol (teks per baris, 115200 baud) — lihat juga esp32_link.py:
 *   Mini PC -> ESP32 : PING | STATUS | SERVO:<deg> | BEEP:<ms>:<n> | ALERT:<ms>
 *                      LCD:<b1>|<b2>|<b3>|<b4> | LCDCLR
 *   ESP32 -> Mini PC : READY:<nama> | PONG | ST:<ir1>:<ir2>:<btn>
 *                      EVT:IR1:<0|1> | EVT:IR2:<0|1> | EVT:BTN:<0|1> | ERR:<pesan>
 *                      ERR:BTN_FAULT (tombol ditekan > BUTTON_FAULT_MS / kabel putus)
 *                      EVT:BTN_OK    (tombol kembali normal setelah BTN_FAULT)
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
#define PIN_RELAY       26
#define PIN_SDA         21
#define PIN_SCL         22

// ── Parameter ────────────────────────────────────
const uint32_t SERIAL_BAUD        = 115200;  // Harus sama dengan ESP32_BAUD di config.py
const uint8_t  LCD_COLS           = 16;
const uint8_t  LCD_ROWS           = 2;
const uint16_t SERVO_MIN_US       = 500;     // Pulsa 0 derajat  (2.5% duty @50Hz)
const uint16_t SERVO_MAX_US       = 2500;    // Pulsa 180 derajat (12.5% duty @50Hz)
const uint32_t SERVO_SETTLE_MS    = 500;     // Waktu servo mencapai posisi sebelum sinyal dilepas
const bool     SERVO_DETACH_IDLE  = true;    // Lepas sinyal setelah bergerak agar tidak jitter / panas
const uint32_t IR_DEBOUNCE_MS     = 30;
const uint32_t BUTTON_DEBOUNCE_MS = 50;
const bool     BUTTON_IS_NC       = true;    // true = push button NC (XB7NA42), false = NO
const uint32_t BUTTON_FAULT_MS    = 5000;    // Ditekan terus lebih dari ini = gangguan / kabel putus
const uint32_t HEARTBEAT_MS       = 250;     // Interval kirim status ST:...
const uint32_t PC_TIMEOUT_MS      = 5000;    // Tampilkan peringatan LCD jika Mini PC diam selama ini
const uint16_t BEEP_GAP_MS        = 80;
const bool     RELAY_ACTIVE_LOW   = true;    // true = modul relay Active LOW (HIGH = idle/mati, LOW = kontak/nyala)
const uint8_t  RELAY_ON           = RELAY_ACTIVE_LOW ? LOW  : HIGH;
const uint8_t  RELAY_OFF          = RELAY_ACTIVE_LOW ? HIGH : LOW;

// ── Input dengan Debounce ────────────────────────
struct DebouncedInput {
  uint8_t  pin;
  uint32_t debounceMs;
  bool     inverted;   // false = aktif saat LOW (NO / sensor IR), true = aktif saat HIGH (NC)
  bool     stable;     // true = aktif (terhalang / ditekan)
  bool     lastRaw;
  uint32_t changedAt;
};

DebouncedInput irBefore = {PIN_IR_BEFORE, IR_DEBOUNCE_MS,     false,        false, false, 0};
DebouncedInput irAfter  = {PIN_IR_AFTER,  IR_DEBOUNCE_MS,     false,        false, false, 0};
DebouncedInput button   = {PIN_BUTTON,    BUTTON_DEBOUNCE_MS, BUTTON_IS_NC, false, false, 0};

// Baca level pin dan terjemahkan ke "aktif" sesuai jenis kontak
bool readActive(const DebouncedInput &in) {
  return (digitalRead(in.pin) == LOW) != in.inverted;
}

// Return true jika status stabil berubah
bool updateInput(DebouncedInput &in) {
  bool raw = readActive(in);
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

// ── Proteksi Push Button (kabel putus pada kontak NC) ──
bool     buttonFault     = false;  // true = ditekan terus > BUTTON_FAULT_MS, EVT:BTN:1 ditahan
uint32_t buttonPressedAt = 0;      // Waktu tombol mulai terbaca ditekan (stabil)

void updateButton() {
  if (updateInput(button)) {
    if (button.stable) {
      buttonPressedAt = millis();
      // Tekanan normal dikirim seketika setelah debounce, tidak menunggu BUTTON_FAULT_MS
      if (!buttonFault) Serial.println("EVT:BTN:1");
    } else if (buttonFault) {
      buttonFault = false;
      Serial.println("EVT:BTN_OK");
    } else {
      Serial.println("EVT:BTN:0");
    }
  }

  if (button.stable && !buttonFault && millis() - buttonPressedAt >= BUTTON_FAULT_MS) {
    buttonFault = true;
    Serial.println("ERR:BTN_FAULT");
  }
}

// ── Buzzer Non-Blocking ──────────────────────────
struct BuzzerPattern {
  uint16_t onMs;
  uint16_t offMs;
  uint8_t  remaining;
  bool     on;
  uint32_t nextAt;
} buzzer = {0, 0, 0, false, 0};

void setBuzzerContinuous(bool on) {
  buzzer = {0, 0, 0, false, 0};
  digitalWrite(PIN_RELAY, on ? RELAY_ON : RELAY_OFF);
}

void startBuzzer(uint16_t onMs, uint16_t offMs, uint8_t count) {
  digitalWrite(PIN_RELAY, RELAY_OFF);
  buzzer = {onMs, offMs, count, false, millis()};
}

void updateBuzzer() {
  if (!buzzer.on && buzzer.remaining == 0) return;
  uint32_t now = millis();
  if ((int32_t)(now - buzzer.nextAt) < 0) return;

  if (buzzer.on) {
    digitalWrite(PIN_RELAY, RELAY_OFF);
    buzzer.on = false;
    buzzer.remaining--;
    buzzer.nextAt = now + buzzer.offMs;
  } else if (buzzer.remaining > 0) {
    digitalWrite(PIN_RELAY, RELAY_ON);
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
String   lcdLines[LCD_ROWS];   // Isi terakhir yang diminta Mini PC
bool     lcdOverlay = false;   // true saat layar menampilkan peringatan "Mini PC offline"
uint32_t lastLcdRetryMs = 0;
const uint32_t LCD_RETRY_MS = 10000;

uint8_t scanI2cLcdAddress(bool fullScan) {
  // Prioritaskan alamat umum PCF8574 (0x27) & PCF8574A (0x3F)
  const uint8_t common[] = {0x27, 0x3F};
  const uint8_t all[] = {
    0x27, 0x3F,
    0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26,
    0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E
  };
  if (!fullScan) {
    for (uint8_t addr : common) {
      Wire.beginTransmission(addr);
      if (Wire.endTransmission() == 0) return addr;
    }
    return 0;
  }
  for (uint8_t addr : all) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) return addr;
  }
  return 0;
}

void recoverI2cBus() {
  // Jika LCD ditenagai PSU eksternal & ESP32 reset di tengah transaksi I2C,
  // slave PCF8574 bisa menahan SDA LOW. Kirim 9 pulsa clock SCL untuk melepaskannya.
  pinMode(PIN_SDA, INPUT_PULLUP);
  pinMode(PIN_SCL, OUTPUT);
  for (uint8_t i = 0; i < 9; i++) {
    digitalWrite(PIN_SCL, LOW);
    delayMicroseconds(10);
    digitalWrite(PIN_SCL, HIGH);
    delayMicroseconds(10);
  }
  pinMode(PIN_SDA, OUTPUT);
  digitalWrite(PIN_SDA, LOW);
  delayMicroseconds(10);
  digitalWrite(PIN_SDA, HIGH);
  delayMicroseconds(10);
}

void lcdRedrawAll();

bool tryInitLcd(bool fullScan = false) {
  if (lcd) return true;
  uint8_t lcdAddr = scanI2cLcdAddress(fullScan);
  if (!lcdAddr) return false;

  lcd = new LiquidCrystal_I2C(lcdAddr, LCD_COLS, LCD_ROWS);
  lcd->begin();
  Wire.setTimeOut(20);
  lcd->backlight();
  lcdRedrawAll();
  Serial.printf("READY:LCD_OK_0x%02X\n", lcdAddr);
  return true;
}

String padLine(const String &s) {
  String out = s.substring(0, LCD_COLS);
  while (out.length() < LCD_COLS) out += ' ';
  return out;
}

void lcdWriteRow(uint8_t row, const String &text) {
  if (!lcd || row >= LCD_ROWS) return;
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
  } else if (cmd == "BUZZ:ON") {
    setBuzzerContinuous(true);
  } else if (cmd == "BUZZ:OFF") {
    setBuzzerContinuous(false);
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
    lcdWriteRow(0, "MINI PC OFFLINE!");
    lcdWriteRow(1, "HUBUNGI PETUGAS ");
    if (LCD_ROWS >= 4) {
      lcdWriteRow(2, "PALANG TIDAK    ");
      lcdWriteRow(3, "BERUBAH POSISI  ");
    }
  }
}

// ── Setup & Loop ─────────────────────────────────
void setup() {
  Serial.begin(SERIAL_BAUD);

  pinMode(PIN_IR_BEFORE, INPUT_PULLUP);
  pinMode(PIN_IR_AFTER, INPUT_PULLUP);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  // Set RELAY_OFF sebelum pinMode OUTPUT agar relay tidak sempat berkontak saat boot
  digitalWrite(PIN_RELAY, RELAY_OFF);
  pinMode(PIN_RELAY, OUTPUT);
  digitalWrite(PIN_RELAY, RELAY_OFF);

  // Servo tidak digerakkan saat boot: jika ESP32 reset ketika kendaraan
  // berada di bawah palang, posisi palang dibiarkan hingga Mini PC memberi perintah.
  ESP32PWM::allocateTimer(0);
  gateServo.setPeriodHertz(50);

  lcdLines[0] = padLine("GERBANG KELUAR");
  lcdLines[1] = padLine("TUNGGU MINI PC..");

  delay(100);  // Beri waktu catu daya 5V modul I2C LCD stabil saat baru dicolok
  recoverI2cBus();
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setTimeOut(20);
  tryInitLcd(true);
  lastLcdRetryMs = millis();

  irBefore.stable = irBefore.lastRaw = readActive(irBefore);
  irAfter.stable  = irAfter.lastRaw  = readActive(irAfter);
  button.stable   = button.lastRaw   = readActive(button);
  // Tombol sudah terbaca ditekan saat boot: tidak dikirim sebagai EVT:BTN:1 (bukan transisi).
  // Jika terus ditekan, BTN_FAULT terpicu setelah BUTTON_FAULT_MS; tekanan pertama
  // baru diterima setelah tombol terbaca lepas.
  buttonPressedAt = millis();

  Serial.println("READY:SMART_GATE_ESP32_V1");
  if (!lcd) Serial.println("ERR:LCD_NOT_FOUND");
  sendStatus();
}

void loop() {
  readSerial();

  if (updateInput(irBefore)) Serial.printf("EVT:IR1:%d\n", irBefore.stable);
  if (updateInput(irAfter))  Serial.printf("EVT:IR2:%d\n", irAfter.stable);
  updateButton();

  updateBuzzer();
  updateServo();
  checkPcTimeout();

  // Jika LCD belum terdeteksi saat boot (misal kabel jumper longgar), coba deteksi ulang tiap 3 detik
  if (!lcd && millis() - lastLcdRetryMs >= LCD_RETRY_MS) {
    lastLcdRetryMs = millis();
    tryInitLcd();
  }

  if (millis() - lastHeartbeat >= HEARTBEAT_MS) {
    lastHeartbeat = millis();
    sendStatus();
  }
}
