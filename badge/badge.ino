// HappyClinic badge — Arduino Nano ESP32.
// Polls the dashboard every 3 s at /<PATIENT_ID>/nps and renders a Zelda-
// style health bar on the 1.3" SH1106 OLED (3 hearts -> calm, 1 -> high
// distress, broken heart + INTERVENE -> clinical escalation).
//
// Build / flash (WSL2):
//   arduino-cli lib install "U8g2"
//   arduino-cli compile --fqbn arduino:esp32:nano_nora badge
//   arduino-cli upload  --fqbn arduino:esp32:nano_nora -p /dev/ttyACM0 badge
//   arduino-cli monitor -p /dev/ttyACM0 -c baudrate=115200

#include <Arduino.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ESPmDNS.h>

// ---------------- Config (edit in place) -------------------------------------
#define WIFI_SSID   "mixpanel-guest"
#define WIFI_PASS   "analytics"

// Flask dashboard host. Raw IP on the same 2.4 GHz network — here it's the
// Windows host (172.16.23.107) with a netsh portproxy to the WSL2 Flask app.
#define SERVER_HOST "172.16.23.107"
#define SERVER_PORT 5050

// Which patient this badge displays. Must match a key in tools/dashboard.py.
#define PATIENT_ID  "mark"

// ---------------- Hardware ---------------------------------------------------
// 1.3" SH1106 128x64 on I2C. Wire.setPins(D3, D2) matches the reference sketch
// at /home/dmhardin/projects/nano_hackathon/smiley/smiley.ino.
U8G2_SH1106_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);

// ---------------- State ------------------------------------------------------
static const unsigned long POLL_MS = 3000;
static const unsigned long DRAW_MS = 250;

static int     healthScore   = 3;      // 0..3; 3 = calm, 0 = intervention
static bool    intervention  = false;
static String  patientName   = PATIENT_ID;
static bool    lastFetchOk   = false;
static unsigned long lastPoll = 0;
static unsigned long lastDraw = 0;

// ---------------- Heart primitives -------------------------------------------
// Two humps (discs) joined to a downward triangle. `s` is nominal width.
static void drawHeartFilled(int cx, int cy, int s) {
  int hr = s / 4;
  int hy = cy - s / 6;
  int tipY = cy + s / 2;
  oled.drawDisc(cx - hr, hy, hr);
  oled.drawDisc(cx + hr, hy, hr);
  oled.drawTriangle(cx - s / 2, hy, cx + s / 2, hy, cx, tipY);
}

static void drawHeartOutline(int cx, int cy, int s) {
  int hr = s / 4;
  int hy = cy - s / 6;
  int tipY = cy + s / 2;
  oled.drawCircle(cx - hr, hy, hr);
  oled.drawCircle(cx + hr, hy, hr);
  oled.drawLine(cx - s / 2, hy, cx, tipY);
  oled.drawLine(cx + s / 2, hy, cx, tipY);
}

// Filled heart with a zigzag crack erased through the middle.
static void drawBrokenHeart(int cx, int cy, int s) {
  int hr = s / 4;
  int hy = cy - s / 6;
  int tipY = cy + s / 2;

  oled.drawDisc(cx - hr, hy, hr);
  oled.drawDisc(cx + hr, hy, hr);
  oled.drawTriangle(cx - s / 2, hy, cx + s / 2, hy, cx, tipY);

  oled.setDrawColor(0);
  int zig[] = {-2, 2, -2, 2, -1, 1, 0};
  int segs  = sizeof(zig) / sizeof(zig[0]);
  int y0    = hy - hr;
  int ySpan = tipY - y0;
  int prevX = cx + zig[0];
  int prevY = y0;
  for (int i = 1; i < segs; i++) {
    int y = y0 + (ySpan * i) / (segs - 1);
    int x = cx + zig[i];
    oled.drawLine(prevX - 1, prevY, x - 1, y);
    oled.drawLine(prevX,     prevY, x,     y);
    oled.drawLine(prevX + 1, prevY, x + 1, y);
    prevX = x; prevY = y;
  }
  oled.setDrawColor(1);
}

// ---------------- Screen -----------------------------------------------------
static void drawStatusLine() {
  oled.setFont(u8g2_font_6x10_tr);
  oled.drawStr(2, 9, patientName.c_str());

  const char* st;
  if (!WiFi.isConnected())  st = "no wifi";
  else if (!lastFetchOk)    st = "...";
  else                      st = "ok";
  int w = oled.getStrWidth(st);
  oled.drawStr(128 - w - 2, 9, st);

  oled.drawHLine(0, 12, 128);
}

static void drawHeartBar() {
  oled.clearBuffer();
  drawStatusLine();

  if (healthScore <= 0) {
    drawBrokenHeart(64, 34, 38);
    oled.setFont(u8g2_font_6x10_tr);
    const char* msg = "INTERVENE";
    int w = oled.getStrWidth(msg);
    oled.drawStr(64 - w / 2, 62, msg);
  } else {
    const int s = 18;
    const int xs[3] = {28, 64, 100};
    const int cy = 36;
    for (int i = 0; i < 3; i++) {
      if (i < healthScore) drawHeartFilled(xs[i], cy, s);
      else                 drawHeartOutline(xs[i], cy, s);
    }
    oled.setFont(u8g2_font_6x10_tr);
    char lbl[10];
    snprintf(lbl, sizeof(lbl), "%d / 3", healthScore);
    int w = oled.getStrWidth(lbl);
    oled.drawStr(64 - w / 2, 62, lbl);
  }

  oled.sendBuffer();
}

// ---------------- WiFi + polling ---------------------------------------------
static void connectWiFi() {
  if (WiFi.isConnected()) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("wifi: connecting to %s ...\n", WIFI_SSID);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
    delay(250);
  }
  if (WiFi.isConnected()) {
    Serial.printf("wifi: ok ip=%s\n", WiFi.localIP().toString().c_str());
    if (!MDNS.begin("happyclinic-badge")) Serial.println("mdns: init failed");
  } else {
    Serial.println("wifi: TIMEOUT");
  }
}

// Tiny JSON scrapers — avoid pulling in ArduinoJson for three fields.
static bool extractInt(const String& src, const char* key, int* out) {
  int i = src.indexOf(key);
  if (i < 0) return false;
  int c = src.indexOf(':', i);
  if (c < 0) return false;
  int j = c + 1;
  while (j < (int)src.length() && (src[j] == ' ' || src[j] == '\t')) j++;
  if (j >= (int)src.length()) return false;
  bool neg = false;
  if (src[j] == '-') { neg = true; j++; }
  if (j >= (int)src.length() || !isdigit((unsigned char)src[j])) return false;
  int v = 0;
  while (j < (int)src.length() && isdigit((unsigned char)src[j])) {
    v = v * 10 + (src[j] - '0'); j++;
  }
  *out = neg ? -v : v;
  return true;
}

static bool extractBool(const String& src, const char* key, bool* out) {
  int i = src.indexOf(key);
  if (i < 0) return false;
  int c = src.indexOf(':', i);
  if (c < 0) return false;
  int t = src.indexOf("true",  c);
  int f = src.indexOf("false", c);
  if (t > 0 && (f < 0 || t < f) && t - c < 12) { *out = true;  return true; }
  if (f > 0 && f - c < 12)                     { *out = false; return true; }
  return false;
}

static bool extractString(const String& src, const char* key, String* out) {
  int i = src.indexOf(key);
  if (i < 0) return false;
  int c = src.indexOf(':', i);
  if (c < 0) return false;
  int q1 = src.indexOf('"', c);
  if (q1 < 0) return false;
  int q2 = src.indexOf('"', q1 + 1);
  if (q2 <= q1) return false;
  *out = src.substring(q1 + 1, q2);
  return true;
}

static bool pollNPS() {
  if (!WiFi.isConnected()) return false;
  HTTPClient http;
  char url[160];
  snprintf(url, sizeof(url), "http://%s:%d/%s/nps",
           SERVER_HOST, SERVER_PORT, PATIENT_ID);
  if (!http.begin(url)) { Serial.println("http begin failed"); return false; }
  http.setTimeout(2500);
  int code = http.GET();
  if (code != 200) {
    Serial.printf("nps http=%d url=%s\n", code, url);
    http.end();
    return false;
  }
  String body = http.getString();
  http.end();

  int    score = healthScore;
  bool   iv    = intervention;
  String name  = patientName;
  if (!extractInt(body, "\"score\"", &score)) {
    Serial.println("nps: no score in body");
    return false;
  }
  extractBool(body, "\"intervention\"", &iv);
  extractString(body, "\"name\"", &name);

  if (score < 0) score = 0;
  if (score > 3) score = 3;
  healthScore  = score;
  intervention = iv;
  patientName  = name;
  Serial.printf("nps: %s score=%d intervene=%d\n",
                name.c_str(), score, iv ? 1 : 0);
  return true;
}

// ---------------- Setup / loop -----------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n--- happyclinic badge: hearts ---");

  Wire.setPins(D3, D2);
  oled.begin();
  oled.setBusClock(400000);

  oled.clearBuffer();
  oled.setFont(u8g2_font_6x10_tr);
  oled.drawStr(2, 14, "happyclinic badge");
  oled.drawStr(2, 30, "wifi: ");
  oled.drawStr(40, 30, WIFI_SSID);
  oled.drawStr(2, 46, "patient: ");
  oled.drawStr(54, 46, PATIENT_ID);
  oled.sendBuffer();

  connectWiFi();
}

void loop() {
  unsigned long now = millis();

  if (now - lastPoll >= POLL_MS) {
    lastPoll = now;
    if (!WiFi.isConnected()) connectWiFi();
    lastFetchOk = pollNPS();
  }

  if (now - lastDraw >= DRAW_MS) {
    lastDraw = now;
    drawHeartBar();
  }

  delay(10);
}
