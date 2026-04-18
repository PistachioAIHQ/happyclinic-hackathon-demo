# offpuck · reception triage demo

Proactive AI for urgent-care reception. Watches patients (vitals + face) and the
receptionist (wellbeing), and uses Claude as a triage coach that returns prioritized
staff actions every 30 seconds.

Built in a day for a hackathon demo.

## What's in here

```
esp32/
├── src/                  # Arduino firmware (PlatformIO)
│   └── main.cpp          # BLE HR client + OLED face + serial protocol
├── platformio.ini        # Board: esp32dev, lib: U8g2
├── tools/
│   ├── dashboard.py      # Flask app — the whole reception UI
│   └── emotion_stream.py # CLI-only emotion classifier (pre-dashboard, kept as reference)
└── README.md
```

## Dashboard at a glance

**Left column — Receptionist Focus**
- Live FaceTime camera of the staff member
- Expression → distress index, session timer, break flag
- Claude triage coach card (the climax of the demo)

**Right column — Waiting Room Operations**
1. Stats tiles: patients waiting, average wait, elevated-anxiety count
2. 3D waiting room (Three.js) — patient avatars at their bays with anxiety rings pulsing at HR rate
3. POV camera (iPhone via Continuity Camera) — face recognition + claim flow for unknown visitors
4. Patient accordion — click to expand full EHR and vitals; `open record →` for a dedicated per-patient page at `/patient/<id>`

Anxiety is classified into `calm / balanced / elevated` from HRV RMSSD when RR
intervals are available, or an HR standard-deviation proxy otherwise. Garmin's
Broadcast HR mode omits RR intervals in most cases — start an activity on the
watch to get true HRV.

## Hardware

- ESP32 DevKit (tested with CP2102 on `/dev/cu.usbserial-0001`)
- SH1106 128×64 I2C OLED
- Common-cathode RGB LED on GPIO 32/33/4
- Garmin watch (or any BLE HR strap — standard HR profile `0x180D / 0x2A37`)

Firmware scans for a HR-broadcasting device, subscribes, and reports HR + RR
intervals to the host over serial. It also accepts `emotion:<label>\n` commands
from the host and renders a matching cartoon face on the OLED.

## Host setup (macOS)

```bash
# Python deps
pip3 install --user flask pyserial anthropic

# Set your key (for the Claude triage coach)
export ANTHROPIC_API_KEY=sk-ant-...

# Plug in the ESP32 (CP2102 USB-UART). Confirm port:
ls /dev/cu.usbserial-*

# Flash firmware (one-time, requires PlatformIO)
brew install platformio
cd esp32
cp src/secrets.h.example src/secrets.h   # edit WIFI_SSID / WIFI_PASS if needed
pio run -t upload

# Run dashboard
python3 tools/dashboard.py
# open http://127.0.0.1:5050
```

On the iPhone (for the POV camera): **Settings → General → AirPlay & Handoff → Continuity Camera** ON. Put the phone landscape near the Mac, and select it from the POV dropdown in the dashboard.

## Demo script (60–90 s)

1. **Hook** — "Urgent care reception is chaos. The receptionist is the single point of failure."
2. **Split the screen** — point to the two column headers. Left = staff wellbeing. Right = patient triage.
3. **Walk the right column 1→4** — stats → 3D → POV → patient records.
4. **Click "run triage now"** — Claude reads every patient's vitals, the receptionist's state, and returns prioritized actions. `[HIGH] Mark Chen: escort immediately — chest pain + SOB.` If the receptionist is stressed, it also generates a staff-facing action.
5. **Close** — "We don't just watch patients. We guide staff. That's the proactive layer urgent care is missing."

## Architecture

- **ESP32 firmware (`src/main.cpp`)** — BLE scan + connect to Garmin, parse HR Measurement (flags byte + HR + RR intervals), render OLED face, react to `emotion:` serial commands.
- **Host backend (`tools/dashboard.py`)** — Flask app. Owns in-memory patient roster, runs simulator threads for non-real patients, reads ESP32 serial, forwards descriptors for face matching, calls Claude for triage.
- **Frontend** — served inline from the Flask app. Uses face-api.js (face detection + expressions + 128-D descriptors) and Three.js (3D waiting room).

### Serial protocol

Host → ESP32: `emotion:<happy|sad|angry|surprised|neutral>\n`
ESP32 → host: `hr:<bpm>\n`, `rr:<ms>[,<ms>…]\n`

### Flask endpoints

- `GET /` — main reception dashboard
- `GET /patient/<id>` — per-patient detail page
- `GET /status` — full roster + stats + triage + receptionist snapshot (1 Hz poll target)
- `POST /face` — submit a 128-D descriptor, get the matched patient (or null)
- `POST /claim` — assign the current descriptor to an existing patient record
- `POST /emotion` — forward an emotion to the ESP32 + log to the current visitor
- `POST /receptionist` — log an expression from the staff camera
- `POST /triage` — trigger Claude Sonnet 4.6 on the whole roster
- `POST /reset` — clear all enrollments (for demo re-runs)

## Roster

Edit `PATIENTS` in `tools/dashboard.py` to add/change records. Real vitals
source is the ESP32 (routes to the first patient with `vitals_source: "real"`).
Simulated patients are driven by `simulate_patient()` threads — pick profile
`sim_anxious` or `sim_normal`.

## Known limits

- Anxiety proxy (HR stdev) is directional only — not real HRV. Classification
  on this branch is marked `proxy` in the UI.
- Face recognition uses face-api.js in-browser with a cosine-distance threshold
  of 0.55. Auto-enroll is disabled — visitors must be claimed explicitly.
- ESP32 BLE stack can flake on first connect attempt; firmware handles cleanup
  and rescan. Garmin `Broadcast HR` only includes RR intervals while an activity
  is running on most models.

## License

Internal / demo. Don't ship to production without clinical review and a HIPAA
story.
