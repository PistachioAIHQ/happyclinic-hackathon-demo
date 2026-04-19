# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

HappyClinic is a one-day hackathon demo of proactive AI for urgent-care reception. On the `esp-badge-hearts` branch the firmware is a **WiFi-only badge** that displays a Zelda-style heart bar for one patient; the receptionist dashboard and triage coach on the host remain.

- **Badge firmware** (`badge/badge.ino`) — Arduino Nano ESP32 sketch. Joins WiFi, polls `GET /<PATIENT_ID>/nps` every 3 s, renders the result on a 1.3" SH1106 OLED: 3 filled hearts at score 3 (calm), fewer filled hearts as distress rises, and a large broken heart + `INTERVENE` label at score 0.
- **Flask dashboard** (`tools/dashboard.py`) — single Python file that owns host logic: patient roster, simulator threads, face-matching endpoint, Claude triage calls, inline-HTML UI (`HTML` ≈ line 604, `PATIENT_HTML` ≈ 2063), and the new `/<pid>/nps` endpoint.

`tools/emotion_stream.py` is a pre-dashboard CLI sketch (webcam → Claude vision → OLED) kept as a reference — not part of the main demo path.

## Hardware

- **Board**: Arduino Nano ESP32 (FQBN `arduino:esp32:nano_nora`). USB CDC — enumerates as `/dev/ttyACM*` on Linux. `/dev/cu.usbserial-*` is only relevant to the legacy DevKit branch.
- **Display**: 1.3" I²C SH1106 (128×64) at `0x3C`. Sketch calls `Wire.setPins(D3, D2)` before `oled.begin()` — matches the working reference at `/home/dmhardin/projects/nano_hackathon/smiley/smiley.ino`.
- **Host environment**: WSL2. USB serial isn't visible to the VM by default — attach with `usbipd-win` on Windows (`usbipd list`, then `usbipd attach --wsl --busid <id>`), and the port appears as `/dev/ttyACM0`.

## Common commands

```bash
# one-time
arduino-cli core install arduino:esp32
arduino-cli lib  install "U8g2"

# flash the badge (after usbipd attach)
arduino-cli compile --fqbn arduino:esp32:nano_nora badge
arduino-cli upload  --fqbn arduino:esp32:nano_nora -p /dev/ttyACM0 badge
arduino-cli monitor -p /dev/ttyACM0 -c baudrate=115200

# host dashboard
pip3 install --user flask pyserial anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python3 tools/dashboard.py               # binds 0.0.0.0:5050 so the badge can reach it
```

No test runner or linter is configured.

## Architecture notes

**`/<pid>/nps` endpoint.** `tools/dashboard.py::get_nps` collapses the patient's `distress` (0–1 from rolling emotion window) + `anxiety_from_hrv` level (0/1/2) into a single 0–3 `score` via `badness = distress + anx_level * 0.5`. Response is JSON — `{id, name, score, distress, anxiety, intervention}` — but the firmware only needs `score`, `intervention`, and `name` and parses them with tiny substring scrapers rather than ArduinoJson.

**Badge config.** `WIFI_SSID`, `WIFI_PASS`, `SERVER_HOST`, `SERVER_PORT`, and `PATIENT_ID` are compile-time `#define`s at the top of `badge/badge.ino`. Edit in place and recompile. `SERVER_HOST` defaults to `happyclinic.local` — works if the Mac running the dashboard advertises that Bonjour hostname; otherwise set it to the Mac's LAN IP.

**Heart rendering.** `drawHeartFilled` / `drawHeartOutline` / `drawBrokenHeart` build hearts out of U8g2 primitives (two `drawDisc` humps + `drawTriangle` body). `drawBrokenHeart` draws a full heart then uses `setDrawColor(0)` to erase a zigzag crack down the middle.

**Patient model (`tools/dashboard.py`).** `PATIENTS` dict near the top is the seeded roster. Each entry has a `vitals_source`: `"real"` means ESP32 serial feeds HR/RR (legacy BLE-relay path — inert on the badge branch since the sketch no longer writes to serial), and `sim_anxious` / `sim_normal` means a `simulate_patient` thread drives it using a profile from `SIM_PROFILES`. `mark` with `sim_anxious` reliably reaches `score == 0` during its emotion cycle, which is why it's the default badge target.

**Anxiety classification.** `hrv_rmssd()` prefers true RMSSD from RR intervals and falls back to HR stdev as a *proxy* (flagged with `proxy: true` in the UI). Garmin Broadcast HR only emits RR while an activity is running on most models.

**State & concurrency.** All shared state (vitals deques, `PATIENTS[*]["descriptor"]`, receptionist history) lives behind `state_lock`. Serial writes are behind `ser_lock`. Daemon threads started in `__main__`: `serial_reader`, one `simulate_patient` per sim profile.

**Frontend is inline.** No build step, no separate asset directory — editing the UI means editing the triple-quoted `HTML` / `PATIENT_HTML` strings in `dashboard.py`. Three.js and face-api.js are loaded from CDN. Dashboard polls `/status` at 1 Hz.

## Endpoints (all on `dashboard.py`)

`GET /` main dashboard · `GET /patient/<id>` detail page · `GET /<pid>/nps` badge score (0–3 JSON) · `GET /status` full snapshot (1 Hz poll target) · `POST /face` descriptor match · `POST /claim` enroll descriptor to patient · `POST /emotion` forward to ESP32 + log to current visitor · `POST /receptionist` log staff-camera emotion · `POST /triage` trigger Claude · `POST /reset` clear enrollments.

## Known gotchas

- Using an SSD1306 driver against the 1.3" SH1106 panel produces a shifted image with vertical garbage on the right — if you see that, the controller is wrong, not the wiring. Prefer `U8G2_SH1106_128X64_NONAME_F_HW_I2C`.
- Don't commit secrets: `ANTHROPIC_API_KEY` lives only in the shell env. The WiFi creds in `badge.ino` are a guest network and are intentionally inline.
- The hardcoded `PORT = "/dev/cu.usbserial-0001"` in `tools/dashboard.py` is a macOS leftover from the BLE-relay era. On this branch the serial path is unused by the badge, but `serial_reader` still tries to open it — harmless, just logs "serial open failed" on Linux.
