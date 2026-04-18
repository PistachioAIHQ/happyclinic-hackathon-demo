#include <Arduino.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>

// OLED (SH1106 128x64 I2C)
U8G2_SH1106_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);

// RGB LED
const int PIN_R = 32;
const int PIN_G = 33;
const int PIN_B = 4;
const bool COMMON_ANODE = false;

// Standard BLE Heart Rate service + characteristic
static BLEUUID hrServiceUUID((uint16_t)0x180D);
static BLEUUID hrCharUUID((uint16_t)0x2A37);

// State
static BLEAdvertisedDevice* hrDevice = nullptr;
static BLEClient* bleClient = nullptr;
static bool bleConnected = false;
static bool bleScanning = false;
static uint8_t currentHR = 0;
static String emotion = "waiting";

// Serial-input buffer (host -> ESP32 emotion commands)
static String rxBuf;

void setColor(bool r, bool g, bool b) {
  digitalWrite(PIN_R, COMMON_ANODE ? !r : r);
  digitalWrite(PIN_G, COMMON_ANODE ? !g : g);
  digitalWrite(PIN_B, COMMON_ANODE ? !b : b);
}

void setHRColor(uint8_t hr) {
  if (hr == 0)       setColor(false, false, true);
  else if (hr < 80)  setColor(false, true,  false);
  else if (hr < 110) setColor(true,  true,  false);
  else               setColor(true,  false, false);
}

// ------- Face rendering -------
void drawFace(int cx, int cy, int r, const String& e) {
  oled.drawCircle(cx, cy, r);

  int elx = cx - 7, erx = cx + 7, ey = cy - 3;

  if (e == "surprised") {
    oled.drawCircle(elx, ey, 3);
    oled.drawCircle(erx, ey, 3);
  } else if (e == "love") {
    for (int d = 0; d <= 2; d++) {
      oled.drawLine(elx - 3 + d, ey - 1, elx, ey + 2);
      oled.drawLine(elx + 3 - d, ey - 1, elx, ey + 2);
      oled.drawLine(erx - 3 + d, ey - 1, erx, ey + 2);
      oled.drawLine(erx + 3 - d, ey - 1, erx, ey + 2);
    }
  } else if (e == "angry") {
    oled.drawDisc(elx, ey + 1, 2);
    oled.drawDisc(erx, ey + 1, 2);
    oled.drawLine(elx - 5, ey - 5, elx + 5, ey - 2);
    oled.drawLine(erx - 5, ey - 2, erx + 5, ey - 5);
  } else if (e == "sad") {
    oled.drawDisc(elx, ey + 2, 2);
    oled.drawDisc(erx, ey + 2, 2);
    oled.drawLine(elx - 5, ey - 3, elx + 5, ey - 1);
    oled.drawLine(erx - 5, ey - 1, erx + 5, ey - 3);
  } else if (e == "excited") {
    // big open eyes
    oled.drawCircle(elx, ey, 3);
    oled.drawCircle(erx, ey, 3);
    oled.drawDisc(elx, ey, 1);
    oled.drawDisc(erx, ey, 1);
  } else if (e == "thinking") {
    // one squint + one normal
    oled.drawLine(elx - 3, ey, elx + 3, ey);
    oled.drawDisc(erx, ey, 2);
  } else {
    oled.drawDisc(elx, ey, 2);
    oled.drawDisc(erx, ey, 2);
  }

  int mcx = cx, mcy = cy + 6;
  if (e == "happy" || e == "love") {
    oled.drawLine(mcx - 8, mcy,     mcx - 3, mcy + 5);
    oled.drawLine(mcx - 3, mcy + 5, mcx + 3, mcy + 5);
    oled.drawLine(mcx + 3, mcy + 5, mcx + 8, mcy);
  } else if (e == "excited") {
    // big grin
    oled.drawLine(mcx - 9, mcy - 1, mcx - 4, mcy + 6);
    oled.drawLine(mcx - 4, mcy + 6, mcx + 4, mcy + 6);
    oled.drawLine(mcx + 4, mcy + 6, mcx + 9, mcy - 1);
    oled.drawLine(mcx - 7, mcy + 2, mcx + 7, mcy + 2);  // teeth line
  } else if (e == "sad") {
    oled.drawLine(mcx - 8, mcy + 5, mcx - 3, mcy);
    oled.drawLine(mcx - 3, mcy,     mcx + 3, mcy);
    oled.drawLine(mcx + 3, mcy,     mcx + 8, mcy + 5);
  } else if (e == "surprised") {
    oled.drawCircle(mcx, mcy + 3, 4);
  } else if (e == "angry") {
    oled.drawLine(mcx - 8, mcy + 3, mcx + 8, mcy + 3);
    oled.drawLine(mcx - 5, mcy + 5, mcx - 2, mcy + 1);
    oled.drawLine(mcx + 2, mcy + 1, mcx + 5, mcy + 5);
  } else if (e == "thinking") {
    // off-center smirk
    oled.drawLine(mcx - 6, mcy + 3, mcx + 6, mcy + 4);
  } else {
    oled.drawLine(mcx - 6, mcy + 3, mcx + 6, mcy + 3);
  }
}

void drawOLED() {
  oled.clearBuffer();

  // Face on left (cx=28, cy=26, r=18)
  drawFace(28, 26, 18, emotion);

  // Heart rate on right
  oled.setFont(u8g2_font_ncenB18_tr);
  if (currentHR > 0) {
    char hr[8];
    snprintf(hr, sizeof(hr), "%d", currentHR);
    int w = oled.getStrWidth(hr);
    oled.drawStr(95 - w / 2, 28, hr);
    oled.setFont(u8g2_font_6x10_tr);
    oled.drawStr(95 - oled.getStrWidth("bpm") / 2, 40, "bpm");
  } else {
    oled.setFont(u8g2_font_6x10_tr);
    const char* msg1 = bleConnected ? "HR" : (bleScanning ? "scan..." : "no HR");
    const char* msg2 = bleConnected ? "---" : "";
    oled.drawStr(95 - oled.getStrWidth(msg1) / 2, 24, msg1);
    if (*msg2) oled.drawStr(95 - oled.getStrWidth(msg2) / 2, 36, msg2);
  }

  // Emotion label across bottom
  oled.setFont(u8g2_font_6x10_tr);
  int ew = oled.getStrWidth(emotion.c_str());
  oled.drawStr(64 - ew / 2, 62, emotion.c_str());

  // Divider
  oled.drawHLine(0, 50, 128);

  oled.sendBuffer();
}

// ------- BLE HR -------
static void onHRNotify(BLERemoteCharacteristic* c, uint8_t* data, size_t len, bool isNotify) {
  if (len < 2) return;
  uint8_t flags = data[0];
  size_t idx = 1;
  uint16_t hr;
  if (flags & 0x01) {
    if (len < 3) return;
    hr = data[idx] | (data[idx + 1] << 8);
    idx += 2;
  } else {
    hr = data[idx];
    idx += 1;
  }
  currentHR = hr;
  Serial.printf("hr:%u\n", (unsigned)hr);

  // Skip energy expended if flagged
  if (flags & 0x08) idx += 2;

  // RR intervals — each uint16 LE in 1/1024 s units; may be multiple per frame
  if ((flags & 0x10) && idx + 1 < len) {
    char line[160]; size_t p = 0;
    p += snprintf(line + p, sizeof(line) - p, "rr:");
    bool first = true;
    while (idx + 1 < len && p < sizeof(line) - 10) {
      uint16_t rr1024 = data[idx] | (data[idx + 1] << 8);
      idx += 2;
      unsigned rr_ms = (unsigned)((rr1024 * 1000UL + 512) / 1024);  // round to nearest ms
      p += snprintf(line + p, sizeof(line) - p, "%s%u", first ? "" : ",", rr_ms);
      first = false;
    }
    if (!first) {
      line[p < sizeof(line) - 1 ? p : sizeof(line) - 1] = 0;
      Serial.println(line);
    }
  }
}

class HRScanCallback : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice dev) override {
    if (dev.haveServiceUUID() && dev.isAdvertisingService(hrServiceUUID)) {
      Serial.printf("found HR: %s\n", dev.toString().c_str());
      hrDevice = new BLEAdvertisedDevice(dev);
      BLEDevice::getScan()->stop();
      bleScanning = false;
    }
  }
};

void cleanupBLE() {
  if (bleClient) {
    if (bleClient->isConnected()) bleClient->disconnect();
    delete bleClient;
    bleClient = nullptr;
  }
  if (hrDevice) {
    delete hrDevice;
    hrDevice = nullptr;
  }
  bleConnected = false;
  currentHR = 0;
}

void startScan() {
  bleScanning = true;
  BLEScan* scan = BLEDevice::getScan();
  scan->clearResults();
  scan->start(30, false);
}

bool connectToHR() {
  Serial.println("connecting HR...");
  delay(250);  // let scan fully wind down before GATT connect
  if (bleClient) { delete bleClient; bleClient = nullptr; }
  bleClient = BLEDevice::createClient();

  if (!bleClient->connect(hrDevice)) {
    Serial.println("connect failed; rescanning");
    cleanupBLE();
    startScan();
    return false;
  }

  BLERemoteService* svc = bleClient->getService(hrServiceUUID);
  if (!svc) { Serial.println("no HR service; rescanning"); cleanupBLE(); startScan(); return false; }

  BLERemoteCharacteristic* chr = svc->getCharacteristic(hrCharUUID);
  if (!chr) { Serial.println("no HR char; rescanning"); cleanupBLE(); startScan(); return false; }

  if (chr->canNotify()) chr->registerForNotify(onHRNotify);
  bleConnected = true;
  Serial.println("HR subscribed");
  return true;
}

// ------- Serial host commands -------
void handleLine(String line) {
  line.trim();
  if (line.startsWith("emotion:")) {
    String e = line.substring(8);
    e.toLowerCase();
    e.trim();
    if (e.length()) {
      emotion = e;
      drawOLED();
    }
  }
}

void pollSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (rxBuf.length()) handleLine(rxBuf);
      rxBuf = "";
    } else {
      rxBuf += c;
      if (rxBuf.length() > 80) rxBuf = "";
    }
  }
}

// ------- Setup / loop -------
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n--- offpuck: emotion + HR ---");

  pinMode(PIN_R, OUTPUT);
  pinMode(PIN_G, OUTPUT);
  pinMode(PIN_B, OUTPUT);
  setColor(false, false, false);

  oled.begin();
  drawOLED();

  BLEDevice::init("OffPuck");
  BLEScan* scan = BLEDevice::getScan();
  scan->setAdvertisedDeviceCallbacks(new HRScanCallback());
  scan->setActiveScan(true);
  scan->setInterval(100);
  scan->setWindow(99);
  startScan();
}

unsigned long lastDraw = 0;

void loop() {
  pollSerial();

  if (hrDevice && !bleConnected) {
    connectToHR();  // handles its own cleanup + rescan on failure
  }

  if (bleClient && bleConnected && !bleClient->isConnected()) {
    Serial.println("HR disconnected, rescanning");
    cleanupBLE();
    startScan();
  }

  if (millis() - lastDraw > 500) {
    drawOLED();
    setHRColor(currentHR);
    lastDraw = millis();
  }

  delay(10);
}
