#include <Arduino.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ESPmDNS.h>

#include "secrets.h"

#ifndef WIFI_SSID
#define WIFI_SSID "mixpanel-guest"
#endif
#ifndef WIFI_PASS
#define WIFI_PASS "analytics"
#endif
#ifndef SERVER_HOST
#define SERVER_HOST "happyclinic.local"
#endif
#ifndef SERVER_PORT
#define SERVER_PORT 5050
#endif
#ifndef PATIENT_ID
#define PATIENT_ID "mark"
#endif

// OLED (SH1106 128x64 I2C)
U8G2_SH1106_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);

// RGB LED
const int PIN_R = 32;
const int PIN_G = 33;
const int PIN_B = 4;
const bool COMMON_ANODE = false;

// Poll cadence — user-spec 3s
static const unsigned long POLL_MS = 3000;
static const unsigned long DRAW_MS = 250;

// State
static int healthScore = 3;      // 0..3; 3 = calm, 0 = intervention
static bool intervention = false;
static String patientName = PATIENT_ID;
static bool lastFetchOk = false;
static unsigned long lastPoll = 0;
static unsigned long lastDraw = 0;

// ---------------- LED ---------------------------------------------------------
void setColor(bool r, bool g, bool b) {
  digitalWrite(PIN_R, COMMON_ANODE ? !r : r);
  digitalWrite(PIN_G, COMMON_ANODE ? !g : g);
  digitalWrite(PIN_B, COMMON_ANODE ? !b : b);
}

void setScoreColor(int score) {
  if (score <= 0)      setColor(true,  false, false);   // red -- intervene
  else if (score == 1) setColor(true,  false, false);   // red
  else if (score == 2) setColor(true,  true,  false);   // yellow
  else                 setColor(false, true,  false);   // green
}

// ---------------- Heart primitives -------------------------------------------
// Hearts are two humps (discs) joined to a downward triangle, Zelda-style.
// `s` is the nominal full width/height of the heart in pixels.
void drawHeartFilled(int cx, int cy, int s) {
  int hr = s / 4;                 // hump radius
  int hy = cy - s / 6;            // hump center y
  int tipY = cy + s / 2;
  oled.drawDisc(cx - hr, hy, hr);
  oled.drawDisc(cx + hr, hy, hr);
  oled.drawTriangle(cx - s / 2, hy, cx + s / 2, hy, cx, tipY);
}

void drawHeartOutline(int cx, int cy, int s) {
  int hr = s / 4;
  int hy = cy - s / 6;
  int tipY = cy + s / 2;
  oled.drawCircle(cx - hr, hy, hr);
  oled.drawCircle(cx + hr, hy, hr);
  // Two outer slopes down to the tip, forming the V.
  oled.drawLine(cx - s / 2, hy, cx, tipY);
  oled.drawLine(cx + s / 2, hy, cx, tipY);
  // Small cusp between the humps (the dip in the top of a heart).
  oled.drawLine(cx - hr + hr, hy + hr - 1, cx, hy + 1);
  oled.drawLine(cx, hy + 1, cx + hr - hr, hy + hr - 1);
}

// One large broken heart for intervention state. Draws the heart, then XORs
// a zigzag crack straight down the middle by painting in color 0.
void drawBrokenHeart(int cx, int cy, int s) {
  int hr = s / 4;
  int hy = cy - s / 6;
  int tipY = cy + s / 2;

  // Filled heart body.
  oled.drawDisc(cx - hr, hy, hr);
  oled.drawDisc(cx + hr, hy, hr);
  oled.drawTriangle(cx - s / 2, hy, cx + s / 2, hy, cx, tipY);

  // Zigzag crack, erased through the body so the split is visible.
  oled.setDrawColor(0);
  int zig[] = {-2, 2, -2, 2, -1, 1, 0};   // x offsets top -> tip
  int segs = sizeof(zig) / sizeof(zig[0]);
  int y0 = hy - hr;
  int ySpan = tipY - y0;
  int prevX = cx + zig[0];
  int prevY = y0;
  for (int i = 1; i < segs; i++) {
    int y = y0 + (ySpan * i) / (segs - 1);
    int x = cx + zig[i];
    // 3-pixel-wide erased band to make the crack readable on a 128x64 OLED.
    oled.drawLine(prevX - 1, prevY, x - 1, y);
    oled.drawLine(prevX,     prevY, x,     y);
    oled.drawLine(prevX + 1, prevY, x + 1, y);
    prevX = x; prevY = y;
  }
  oled.setDrawColor(1);
}

// ---------------- Screen ------------------------------------------------------
void drawStatusLine() {
  oled.setFont(u8g2_font_6x10_tr);
  // Left: patient name (truncated if needed).
  char left[20];
  snprintf(left, sizeof(left), "%s", patientName.c_str());
  oled.drawStr(2, 9, left);

  // Right: connection state.
  const char* st;
  if (!WiFi.isConnected())  st = "no wifi";
  else if (!lastFetchOk)    st = "...";
  else                      st = "ok";
  int w = oled.getStrWidth(st);
  oled.drawStr(128 - w - 2, 9, st);

  oled.drawHLine(0, 12, 128);
}

void drawHeartBar() {
  oled.clearBuffer();
  drawStatusLine();

  if (healthScore <= 0) {
    // Intervention: one large broken heart + label.
    drawBrokenHeart(64, 34, 38);
    oled.setFont(u8g2_font_6x10_tr);
    const char* msg = "INTERVENE";
    int w = oled.getStrWidth(msg);
    oled.drawStr(64 - w / 2, 62, msg);
  } else {
    // 3 small hearts, filled from left by score.
    const int s = 18;
    const int xs[3] = {28, 64, 100};
    const int cy = 36;
    for (int i = 0; i < 3; i++) {
      if (i < healthScore) drawHeartFilled(xs[i], cy, s);
      else                 drawHeartOutline(xs[i], cy, s);
    }
    // Score label at the bottom.
    oled.setFont(u8g2_font_6x10_tr);
    char lbl[10];
    snprintf(lbl, sizeof(lbl), "%d / 3", healthScore);
    int w = oled.getStrWidth(lbl);
    oled.drawStr(64 - w / 2, 62, lbl);
  }

  oled.sendBuffer();
}

// ---------------- WiFi + polling ---------------------------------------------
void connectWiFi() {
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

// Tiny JSON scraper — avoids pulling in ArduinoJson for three fields.
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
  if (j >= (int)src.length() || !isdigit(src[j])) return false;
  int v = 0;
  while (j < (int)src.length() && isdigit(src[j])) { v = v * 10 + (src[j] - '0'); j++; }
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
  // Pick whichever comes first after the colon within ~12 chars.
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

bool pollNPS() {
  if (!WiFi.isConnected()) return false;
  HTTPClient http;
  char url[160];
  snprintf(url, sizeof(url), "http://%s:%d/%s/nps", SERVER_HOST, SERVER_PORT, PATIENT_ID);
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

  int score = healthScore;
  bool iv = intervention;
  String name = patientName;
  if (!extractInt(body, "\"score\"", &score)) {
    Serial.println("nps: no score");
    return false;
  }
  extractBool(body, "\"intervention\"", &iv);
  extractString(body, "\"name\"", &name);

  if (score < 0) score = 0;
  if (score > 3) score = 3;
  healthScore = score;
  intervention = iv;
  patientName = name;
  Serial.printf("nps ok: %s score=%d intervene=%d\n", name.c_str(), score, iv ? 1 : 0);
  return true;
}

// ---------------- Setup / loop -----------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n--- happyclinic badge: hearts ---");

  pinMode(PIN_R, OUTPUT);
  pinMode(PIN_G, OUTPUT);
  pinMode(PIN_B, OUTPUT);
  setColor(false, false, true);   // blue = booting

  oled.begin();
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
    setScoreColor(healthScore);
  }

  delay(10);
}
