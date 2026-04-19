# HappyClinic · hackathon demo

Proactive AI for urgent-care reception. Watches patients (vitals + face) and the
receptionist (wellbeing), and uses Claude as a triage coach that returns prioritized
staff actions every 30 seconds.

Built in a day for the OOP Hackathon.

## What's in here

```
badge/
└── badge.ino             # Arduino Nano ESP32 badge; polls /<patient>/nps over Wi-Fi
tools/
├── dashboard.py          # Flask app — reception UI + badge endpoint
└── emotion_stream.py     # CLI-only emotion classifier (kept as reference)
README.md
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

- Arduino Nano ESP32
- SH1106 128×64 I2C OLED
- Wi-Fi network shared with the computer serving Flask

The badge uses Wi-Fi at runtime. It does **not** need USB serial once flashed.
It polls `GET /<patient_id>/nps` every 3 seconds and renders a 0-3 heart score.

## Host setup

### Option A: simplest path on Windows

Flash the badge from Windows, and run Flask on Windows too.

```powershell
# Python deps
py -m pip install flask pyserial anthropic

# Point dashboard.py at the Nano's COM port if you need serial features later.
$env:HAPPYCLINIC_SERIAL_PORT="COM4"
$env:ANTHROPIC_API_KEY="sk-ant-..."

# Run the dashboard from the WSL-backed repo path.
py \\wsl.localhost\Ubuntu\home\dmhardin\projects\happyclinic-hackathon-demo\tools\dashboard.py
```

Open `http://127.0.0.1:5050`.

For the badge sketch, set:

- `WIFI_SSID` / `WIFI_PASS` to your real network
- `SERVER_HOST` to your **Windows machine's LAN IP**
- `PATIENT_ID` to one of `ana`, `mark`, or `priya`

### Option B: keep Flask in WSL

This works too, but the badge cannot usually reach the WSL IP directly from the
LAN. Forward a Windows port into WSL, then point the badge at the Windows LAN IP.

Run Flask in WSL:

```bash
cd /home/dmhardin/projects/happyclinic-hackathon-demo
export ANTHROPIC_API_KEY=sk-ant-...
python3 tools/dashboard.py
```

Then in an elevated PowerShell on Windows, forward port `5050` to the current
WSL address:

```powershell
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=5050 connectaddress=<WSL_IP> connectport=5050
```

If Windows Firewall prompts, allow inbound access for port `5050`.

## Flashing the Nano ESP32

From Windows, use Arduino IDE or `arduino-cli` and the Windows COM port.

Typical commands:

```powershell
arduino-cli compile --fqbn arduino:esp32:nano_nora \\wsl.localhost\Ubuntu\home\dmhardin\projects\happyclinic-hackathon-demo\badge
arduino-cli upload --fqbn arduino:esp32:nano_nora -p COM4 \\wsl.localhost\Ubuntu\home\dmhardin\projects\happyclinic-hackathon-demo\badge
```

WSL can compile, but USB upload is usually much easier from Windows unless you
also set up USB pass-through into WSL.

On the iPhone (for the POV camera): **Settings → General → AirPlay & Handoff → Continuity Camera** ON. Put the phone landscape near the Mac, and select it from the POV dropdown in the dashboard.

## Demo script (60–90 s)

1. **Hook** — "Urgent care reception is chaos. The receptionist is the single point of failure."
2. **Split the screen** — point to the two column headers. Left = staff wellbeing. Right = patient triage.
3. **Walk the right column 1→4** — stats → 3D → POV → patient records.
4. **Click "run triage now"** — Claude reads every patient's vitals, the receptionist's state, and returns prioritized actions. `[HIGH] Mark Chen: escort immediately — chest pain + SOB.` If the receptionist is stressed, it also generates a staff-facing action.
5. **Close** — "We don't just watch patients. We guide staff. That's the proactive layer urgent care is missing."

## Architecture

- **Badge firmware (`badge/badge.ino`)** — joins Wi-Fi, polls `/<patient>/nps`, and renders the heart bar on the OLED.
- **Host backend (`tools/dashboard.py`)** — Flask app. Owns the in-memory patient roster, simulator threads, badge endpoint, and triage flow.
- **Frontend** — served inline from the Flask app. Uses face-api.js and Three.js.

### Badge endpoint

Badge → host: `GET /<patient_id>/nps`

Example response:

```json
{
  "id": "mark",
  "name": "Mark Chen",
  "score": 1,
  "distress": 0.84,
  "anxiety": "elevated",
  "intervention": false
}
```

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

## Notes

- `tools/dashboard.py` now reads these optional env vars:
  `HAPPYCLINIC_SERIAL_PORT`, `HAPPYCLINIC_SERIAL_BAUD`,
  `HAPPYCLINIC_LISTEN_HOST`, `HAPPYCLINIC_LISTEN_PORT`,
  and `HAPPYCLINIC_MODEL`.
- The default serial path is still the old macOS value, so on Windows set
  `HAPPYCLINIC_SERIAL_PORT=COM4` if you want the serial features enabled.

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
