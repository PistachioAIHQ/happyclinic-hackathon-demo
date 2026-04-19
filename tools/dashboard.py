#!/usr/bin/env python3
"""offpuck reception: multi-patient urgent-care triage demo.

Real visitor (face-matched, auto-enrolled on first sight) streams vitals from the
ESP32 + Garmin. Two simulated patients add realistic context for Claude's triage
coach, which watches the whole waiting room and returns prioritized staff actions.
"""
import os
import random
import re
import sqlite3
import struct
import threading
import time
from collections import deque
from typing import Optional

from anthropic import Anthropic
from flask import Flask, Response, jsonify, request
import serial

PORT = os.getenv("HAPPYCLINIC_SERIAL_PORT", "/dev/cu.usbserial-0001")
BAUD = int(os.getenv("HAPPYCLINIC_SERIAL_BAUD", "115200"))
MODEL = os.getenv("HAPPYCLINIC_MODEL", "claude-sonnet-4-6")
LISTEN_HOST = os.getenv("HAPPYCLINIC_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("HAPPYCLINIC_LISTEN_PORT", "5050"))
BADGE_ID = os.getenv("HAPPYCLINIC_BADGE_ID", "badge-1")
BADGE_LABEL = os.getenv("HAPPYCLINIC_BADGE_LABEL", "waiting room badge")
BADGE_DEFAULT_PATIENT = os.getenv("HAPPYCLINIC_BADGE_PATIENT", "david")
FACE_MATCH_THRESHOLD = 0.55    # euclidean distance — 0.4 strict, 0.6 forgiving
# Face-descriptor persistence (SQLite).
FACE_DB_PATH = os.getenv(
    "HAPPYCLINIC_FACE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "faces.db"),
)
# Adaptive learning: when a live match is more confident than this, store the
# new descriptor as an additional sample so a returning patient is recognized
# faster and more accurately next time.
FACE_LEARN_DISTANCE = 0.40
FACE_LEARN_INTERVAL_S = 20.0   # per-patient cooldown between auto-captured samples
FACE_MAX_SAMPLES = 25          # cap samples per patient
SESSION_START = time.time()

EMOTIONS = ["happy", "sad", "angry", "surprised", "neutral"]

# -------- Patient roster (seeded) ---------------------------------------------

PATIENTS: dict[str, dict] = {
    "ciaran": {
        "id": "ciaran",
        "name": "Ciaran Murphy",
        "age": 34,
        "chief_complaint": "Persistent cough, fever 3 days",
        "allergies": "penicillin",
        "meds": "lisinopril 10mg",
        "last_visit": "2024-11-18 (strep, resolved)",
        "bay": "front desk",
        "vitals_source": "real",    # fed by ESP32/Garmin serial
        "checked_in": None,         # set when their face is first enrolled
        "descriptors": [],          # list of 128-float face samples (persisted in SQLite)
    },
    "david": {
        "id": "david",
        "name": "David Hardin",
        "age": 38,
        "sex": "male",
        "chief_complaint": "Sharp chest pain, shortness of breath",
        "allergies": "shellfish",
        "meds": "atorvastatin, aspirin 81mg",
        "last_visit": "2024-06-03 (hypertension follow-up)",
        "bay": "bay 2",
        "vitals_source": "sim_anxious",
        "checked_in": time.time() - 60 * 9,   # 9 min into wait at boot
        "descriptors": [],
    },
    "priya": {
        "id": "priya",
        "name": "Priya Singh",
        "age": 28,
        "chief_complaint": "Sprained ankle, sports injury",
        "allergies": "none",
        "meds": "none",
        "last_visit": "2023-02-11 (annual physical)",
        "bay": "bay 1",
        "vitals_source": "sim_normal",
        "checked_in": time.time() - 60 * 3,   # 3 min into wait at boot
        "descriptors": [],
    },
}

# Per-patient rolling state
def _blank_vitals() -> dict:
    return {
        "hr": 0,
        "hr_ts": 0.0,
        "hr_history": deque(maxlen=600),           # (ts, hr) ~10 min at 1 Hz
        "emotion": None,
        "emotion_history": deque(maxlen=200),      # {ts, emotion}
        "last_seen_ts": None,
        "rr_samples": deque(maxlen=600),           # (ts, rr_ms)
        # Body language (MediaPipe PoseLandmarker on the POV cam).
        "arms_state": "unknown",                   # "open" | "crossed" | "unknown"
        "arms_ts": 0.0,                            # wall time of last "crossed" observation
        "arms_history": deque(maxlen=120),         # {ts, state} debounced transitions
    }

vitals: dict[str, dict] = {pid: _blank_vitals() for pid in PATIENTS}
state_lock = threading.Lock()

# Current patient at the counter (id or None); set by face matching
current_visitor: Optional[str] = None

# Triage history
triage_history: deque = deque(maxlen=20)

# Receptionist state (laptop camera POV of staff)
receptionist: dict = {
    "session_start": time.time(),
    "emotion": None,
    "emotion_history": deque(maxlen=400),
    "last_seen_ts": None,
}

badge_assignments: dict[str, Optional[str]] = {
    BADGE_ID: BADGE_DEFAULT_PATIENT if BADGE_DEFAULT_PATIENT in PATIENTS else None,
}

# -------- Flask + Anthropic ---------------------------------------------------

app = Flask(__name__)
client = Anthropic()
ser: Optional[serial.Serial] = None
ser_lock = threading.Lock()


def open_serial() -> bool:
    global ser
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.25)
        time.sleep(2)
        ser.reset_input_buffer()
        return True
    except Exception as e:
        print(f"serial open failed: {e}")
        ser = None
        return False


def write_serial(cmd: str) -> bool:
    global ser
    with ser_lock:
        if ser is None and not open_serial():
            return False
        try:
            ser.write((cmd + "\n").encode())
            ser.flush()
            return True
        except Exception as e:
            print(f"serial write failed: {e}")
            try: ser.close()
            except Exception: pass
            ser = None
            return False


# -------- Real vitals: ESP32/Garmin -> "ciaran" -------------------------------

def serial_reader() -> None:
    buf = b""
    while True:
        s = ser
        if s is None:
            time.sleep(0.5); continue
        try:
            data = s.read(128)
        except Exception as e:
            print(f"serial read err: {e}")
            time.sleep(0.5); continue
        if not data: continue
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.strip().decode(errors="ignore")
            if not text: continue
            if text.startswith("hr:"):
                try:
                    hr = int(text.split(":", 1)[1])
                    now = time.time()
                    with state_lock:
                        v = vitals["ciaran"]
                        v["hr"] = hr
                        v["hr_ts"] = now
                        if not v["hr_history"] or now - v["hr_history"][-1][0] >= 1.0:
                            v["hr_history"].append((now, hr))
                except ValueError:
                    pass
            elif text.startswith("rr:"):
                try:
                    now = time.time()
                    with state_lock:
                        rrs = vitals["ciaran"]["rr_samples"]
                        for part in text[3:].split(","):
                            rr = int(part.strip())
                            if 300 <= rr <= 2000:
                                rrs.append((now, rr))
                except ValueError:
                    pass
            else:
                print(f"[esp32] {text}")


# -------- Simulated patients --------------------------------------------------

SIM_PROFILES = {
    "sim_anxious": {
        "baseline_hr": 96,
        "hr_noise": 1.0,                  # low variance -> proxy flags elevated anxiety
        "drift": 0.03,
        "emotion_cycle": ["neutral", "sad", "neutral", "surprised", "angry", "neutral", "sad"],
        "emotion_change_p": 0.22,
    },
    "sim_normal": {
        "baseline_hr": 72,
        "hr_noise": 3.6,                  # normal resting variance
        "drift": 0.06,
        "emotion_cycle": ["neutral", "happy", "neutral", "neutral", "happy"],
        "emotion_change_p": 0.15,
    },
}


def simulate_patient(pid: str, profile_name: str) -> None:
    profile = SIM_PROFILES[profile_name]
    hr = float(profile["baseline_hr"])
    emo_idx = 0
    # For HRV realism: generate RR intervals so sim_normal gets true RMSSD too
    rng = random.Random(hash(pid) & 0xFFFFFFFF)
    while True:
        # HR random walk + drift back to baseline
        hr += rng.gauss(0, profile["hr_noise"] * 0.4)
        hr += (profile["baseline_hr"] - hr) * profile["drift"]
        hr = max(45, min(180, hr))
        hr_int = int(round(hr))
        now = time.time()
        with state_lock:
            v = vitals[pid]
            v["hr"] = hr_int
            v["hr_ts"] = now
            if not v["hr_history"] or now - v["hr_history"][-1][0] >= 1.0:
                v["hr_history"].append((now, hr_int))
            # Synthesize 1-2 RR intervals per tick so RMSSD works for sim_normal
            mean_rr = 60000.0 / hr_int
            rr_jitter = profile["hr_noise"] * 8  # ms; maps to similar HRV feel
            for _ in range(2):
                rr = int(round(rng.gauss(mean_rr, rr_jitter)))
                if 300 <= rr <= 2000:
                    v["rr_samples"].append((now, rr))
            # Emotion tick
            if rng.random() < profile["emotion_change_p"]:
                emo = profile["emotion_cycle"][emo_idx % len(profile["emotion_cycle"])]
                emo_idx += 1
                v["emotion"] = emo
                v["emotion_history"].append({"ts": int(now * 1000), "emotion": emo})
        time.sleep(1.0)


# -------- Analytics -----------------------------------------------------------

def hrv_rmssd(pid: str, window_s: float = 60.0) -> Optional[dict]:
    now = time.time()
    with state_lock:
        rrs = [rr for (t, rr) in vitals[pid]["rr_samples"] if now - t <= window_s]
        hrs = [v for (t, v) in vitals[pid]["hr_history"] if now - t <= window_s]
    if len(rrs) >= 4:
        diffs = [rrs[i + 1] - rrs[i] for i in range(len(rrs) - 1)]
        rmssd = (sum(d * d for d in diffs) / len(diffs)) ** 0.5
        return {"source": "rr", "rmssd_ms": round(rmssd, 1), "n": len(rrs)}
    if len(hrs) >= 5:
        mean_h = sum(hrs) / len(hrs)
        stdev = (sum((h - mean_h) ** 2 for h in hrs) / len(hrs)) ** 0.5
        return {"source": "hr_stdev", "hr_stdev_bpm": round(stdev, 2), "n": len(hrs)}
    return None


def anxiety_from_hrv(hrv: Optional[dict]) -> Optional[dict]:
    if not hrv:
        return None
    if hrv["source"] == "rr":
        x = hrv["rmssd_ms"]
        level, label = (2, "elevated") if x < 20 else (1, "balanced") if x < 50 else (0, "calm")
        return {"level": level, "label": label, "metric": f"RMSSD {x} ms", "proxy": False}
    s = hrv.get("hr_stdev_bpm")
    if s is None:
        return None
    level, label = (2, "elevated") if s < 2.0 else (1, "balanced") if s < 4.0 else (0, "calm")
    return {"level": level, "label": label, "metric": f"HR stdev {s} bpm", "proxy": True}


# Crossed arms within this window boosts distress. Window is intentionally a
# little longer than the debounce so transient transitions still count.
ARMS_ACTIVE_WINDOW_S = 20.0
ARMS_DISTRESS_BOOST = 0.15


def arms_active(pid: str) -> bool:
    """True if the patient's last debounced arms state is 'crossed' and fresh."""
    with state_lock:
        v = vitals[pid]
        state = v.get("arms_state")
        ts = v.get("arms_ts", 0.0)
    return state == "crossed" and ts and (time.time() - ts) < ARMS_ACTIVE_WINDOW_S


def distress_index(pid: str, window_s: float = 30.0) -> float:
    """0 (calm/positive) -> 1 (distressed). From rolling emotion labels,
    with a small additive bump if the patient is currently reading as
    arms-crossed (body-language signal from MediaPipe PoseLandmarker)."""
    now_ms = int(time.time() * 1000)
    with state_lock:
        recent = [e for e in vitals[pid]["emotion_history"] if now_ms - e["ts"] <= window_s * 1000]
    if recent:
        weights = {"happy": -1.0, "neutral": 0.0, "sad": 1.0, "angry": 1.0, "surprised": 0.5}
        s = sum(weights.get(e["emotion"], 0.0) for e in recent) / len(recent)
        base = max(0.0, min(1.0, (s + 1.0) / 2.0))
    else:
        base = 0.0
    if arms_active(pid):
        base = min(1.0, base + ARMS_DISTRESS_BOOST)
    return round(base, 2)


def wait_seconds(pid: str) -> Optional[float]:
    ci = PATIENTS[pid]["checked_in"]
    return None if ci is None else round(time.time() - ci, 1)


def receptionist_distress(window_s: float = 30.0) -> float:
    now_ms = int(time.time() * 1000)
    with state_lock:
        recent = [e for e in receptionist["emotion_history"] if now_ms - e["ts"] <= window_s * 1000]
    if not recent:
        return 0.0
    weights = {"happy": -1.0, "neutral": 0.0, "sad": 1.0, "angry": 1.0, "surprised": 0.5}
    s = sum(weights.get(e["emotion"], 0.0) for e in recent) / len(recent)
    return round(max(0.0, min(1.0, (s + 1.0) / 2.0)), 2)


def receptionist_snapshot() -> dict:
    now = time.time()
    with state_lock:
        last_seen = receptionist["last_seen_ts"]
        emotion = receptionist["emotion"]
        session_s = now - receptionist["session_start"]
    return {
        "present": bool(last_seen and now - last_seen < 10),
        "emotion": emotion,
        "distress": receptionist_distress(30.0),
        "session_s": round(session_s, 1),
        "session_min": round(session_s / 60.0, 1),
        "last_seen_s": round(now - last_seen, 1) if last_seen else None,
    }


def patient_snapshot(pid: str) -> dict:
    p = PATIENTS[pid]
    v = vitals[pid]
    hrv = hrv_rmssd(pid)
    anx = anxiety_from_hrv(hrv)
    wait = wait_seconds(pid)
    with state_lock:
        emotion = v["emotion"]
        hr = v["hr"]
        hr_ts = v["hr_ts"]
        last_seen = v["last_seen_ts"]
        history_pts = len(v["hr_history"])
        hr_series = list(v["hr_history"])[-180:]
        arms_state = v.get("arms_state", "unknown")
        arms_ts = v.get("arms_ts", 0.0)
    arms_fresh = arms_state == "crossed" and arms_ts and (time.time() - arms_ts) < ARMS_ACTIVE_WINDOW_S
    arms_report = "crossed" if arms_fresh else (arms_state if arms_state in ("open", "unknown") else "open")
    hr_fresh = hr if hr_ts and time.time() - hr_ts < 10 else None
    return {
        "id": p["id"],
        "name": p["name"],
        "age": p["age"],
        "sex": p.get("sex"),
        "chief_complaint": p["chief_complaint"],
        "allergies": p["allergies"],
        "meds": p["meds"],
        "last_visit": p["last_visit"],
        "bay": p["bay"],
        "vitals_source": p["vitals_source"],
        "enrolled": bool(p["descriptors"]),
        "checked_in": p["checked_in"],
        "wait_s": wait,
        "hr": hr_fresh,
        "hrv": hrv,
        "anxiety": anx,
        "distress": distress_index(pid),
        "emotion": emotion,
        "arms": arms_report,
        "arms_active": bool(arms_fresh),
        "hr_series": [[int(t * 1000), h] for (t, h) in hr_series],
        "history_pts": history_pts,
        "last_seen_s": round(time.time() - last_seen, 1) if last_seen else None,
    }


def nps_payload(pid: str) -> dict:
    """Numeric distress/anxiety score for a patient, intended for the badge."""
    snap = patient_snapshot(pid)
    distress = snap["distress"] or 0.0
    anx = snap["anxiety"]
    anx_level = anx["level"] if anx else 0
    badness = distress + (anx_level * 0.5)  # max ~2.0
    if badness < 0.3:
        score = 3
    elif badness < 0.8:
        score = 2
    elif badness < 1.3:
        score = 1
    else:
        score = 0
    return {
        "id": pid,
        "name": PATIENTS[pid]["name"],
        "score": score,
        "distress": round(distress, 3),
        "anxiety": anx["label"] if anx else None,
        "intervention": score == 0,
    }


def badge_snapshot(badge_id: str) -> dict:
    with state_lock:
        patient_id = badge_assignments.get(badge_id)
    return {
        "id": badge_id,
        "label": BADGE_LABEL if badge_id == BADGE_ID else badge_id,
        "patient_id": patient_id,
        "patient_name": PATIENTS[patient_id]["name"] if patient_id in PATIENTS else None,
    }


# -------- Face recognition ----------------------------------------------------
#
# Descriptors are 128-float face-api.js embeddings. We keep one or more samples
# per patient (list on PATIENTS[pid]["descriptors"]) and persist them to a
# SQLite file so enrollments survive restarts and recognition gets progressively
# faster/tighter as more samples accumulate.

# Auto-captured sample bookkeeping (per-patient cooldown for adaptive learning).
_last_auto_learn: dict[str, float] = {}
face_db_lock = threading.Lock()


def _pack_descriptor(desc: list[float]) -> bytes:
    """Pack a 128-float descriptor as compact little-endian float32 bytes."""
    return struct.pack("<128f", *desc)


def _unpack_descriptor(blob: bytes) -> list[float]:
    if len(blob) != 128 * 4:
        raise ValueError(f"bad descriptor blob: {len(blob)} bytes")
    return list(struct.unpack("<128f", blob))


def _face_db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(FACE_DB_PATH, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_face_db() -> None:
    """Create schema (if needed) and load persisted descriptors into memory."""
    with face_db_lock:
        conn = _face_db_connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS face_descriptors (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id  TEXT    NOT NULL,
                    descriptor  BLOB    NOT NULL,
                    source      TEXT    NOT NULL DEFAULT 'claim',
                    created_at  REAL    NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_face_patient ON face_descriptors(patient_id)"
            )
            rows = conn.execute(
                "SELECT patient_id, descriptor FROM face_descriptors ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()

    loaded: dict[str, int] = {}
    with state_lock:
        for pid, blob in rows:
            if pid not in PATIENTS:
                continue
            try:
                PATIENTS[pid]["descriptors"].append(_unpack_descriptor(blob))
            except ValueError:
                continue
            loaded[pid] = loaded.get(pid, 0) + 1
            if PATIENTS[pid]["checked_in"] is None:
                PATIENTS[pid]["checked_in"] = time.time()
    if loaded:
        summary = ", ".join(f"{pid}:{n}" for pid, n in loaded.items())
        print(f"  face db: loaded samples from {FACE_DB_PATH} ({summary})")
    else:
        print(f"  face db: no persisted samples yet ({FACE_DB_PATH})")


def _db_insert_descriptor(pid: str, desc: list[float], source: str) -> None:
    with face_db_lock:
        conn = _face_db_connect()
        try:
            conn.execute(
                "INSERT INTO face_descriptors (patient_id, descriptor, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                (pid, _pack_descriptor(desc), source, time.time()),
            )
        finally:
            conn.close()


def _db_prune_patient(pid: str, keep: int) -> None:
    """Keep only the most recent `keep` rows for patient `pid`."""
    with face_db_lock:
        conn = _face_db_connect()
        try:
            conn.execute(
                """
                DELETE FROM face_descriptors
                 WHERE patient_id = ?
                   AND id NOT IN (
                       SELECT id FROM face_descriptors
                        WHERE patient_id = ?
                        ORDER BY id DESC
                        LIMIT ?
                   )
                """,
                (pid, pid, keep),
            )
        finally:
            conn.close()


def _db_clear_all() -> None:
    with face_db_lock:
        conn = _face_db_connect()
        try:
            conn.execute("DELETE FROM face_descriptors")
        finally:
            conn.close()


def euclid(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def match_descriptor(desc: list[float]) -> Optional[dict]:
    """Find the closest stored sample across all patients' descriptor lists."""
    best_pid, best_d = None, 9.0
    with state_lock:
        for pid, p in PATIENTS.items():
            for sample in p["descriptors"]:
                d = euclid(desc, sample)
                if d < best_d:
                    best_d = d
                    best_pid = pid
    if best_pid and best_d < FACE_MATCH_THRESHOLD:
        return {"patient_id": best_pid, "distance": round(best_d, 3)}
    return None


def _maybe_auto_learn(pid: str, desc: list[float], distance: float) -> bool:
    """Append a high-confidence live match as an extra persisted sample.

    Keeps at most FACE_MAX_SAMPLES per patient (oldest pruned) so the DB stays
    bounded while recognition gets tighter over repeated visits.
    """
    if distance >= FACE_LEARN_DISTANCE:
        return False
    now = time.time()
    if now - _last_auto_learn.get(pid, 0.0) < FACE_LEARN_INTERVAL_S:
        return False
    with state_lock:
        PATIENTS[pid]["descriptors"].append(desc)
        if len(PATIENTS[pid]["descriptors"]) > FACE_MAX_SAMPLES:
            PATIENTS[pid]["descriptors"] = PATIENTS[pid]["descriptors"][-FACE_MAX_SAMPLES:]
    _last_auto_learn[pid] = now
    try:
        _db_insert_descriptor(pid, desc, source="auto")
        _db_prune_patient(pid, FACE_MAX_SAMPLES)
    except sqlite3.Error as e:
        print(f"  face db: auto-learn insert failed for {pid}: {e}")
        return False
    return True


@app.post("/face")
def post_face():
    """Match a face descriptor. No auto-enrollment — claim explicitly via /claim."""
    global current_visitor
    data = request.get_json(force=True)
    desc = data.get("descriptor")
    if not desc or len(desc) != 128:
        return jsonify({"match": None, "current_visitor": current_visitor})

    match = match_descriptor(desc)
    if match:
        pid = match["patient_id"]
        with state_lock:
            vitals[pid]["last_seen_ts"] = time.time()
        current_visitor = pid
        _maybe_auto_learn(pid, desc, match["distance"])
    else:
        # Face present but unknown — clear current visitor so UI shows claim prompt.
        # Don't wipe a previous match immediately — only if the face persistently
        # fails to match. For simplicity we clear it; frontend throttles the
        # claim prompt based on recent detection.
        current_visitor = None
    return jsonify({"match": match, "current_visitor": current_visitor})


@app.post("/claim")
def post_claim():
    """Assign the provided face descriptor to an existing patient record."""
    global current_visitor
    data = request.get_json(force=True)
    pid = data.get("patient_id")
    desc = data.get("descriptor")
    if pid not in PATIENTS:
        return jsonify({"ok": False, "error": "unknown patient"}), 400
    if not desc or len(desc) != 128:
        return jsonify({"ok": False, "error": "bad descriptor"}), 400
    with state_lock:
        PATIENTS[pid]["descriptors"].append(desc)
        if len(PATIENTS[pid]["descriptors"]) > FACE_MAX_SAMPLES:
            PATIENTS[pid]["descriptors"] = PATIENTS[pid]["descriptors"][-FACE_MAX_SAMPLES:]
        if PATIENTS[pid]["checked_in"] is None:
            PATIENTS[pid]["checked_in"] = time.time()
        vitals[pid]["last_seen_ts"] = time.time()
    try:
        _db_insert_descriptor(pid, desc, source="claim")
        _db_prune_patient(pid, FACE_MAX_SAMPLES)
    except sqlite3.Error as e:
        print(f"  face db: claim insert failed for {pid}: {e}")
    current_visitor = pid
    return jsonify({"ok": True, "patient_id": pid, "name": PATIENTS[pid]["name"]})


@app.post("/receptionist")
def post_receptionist():
    data = request.get_json(force=True)
    emo = (data.get("emotion") or "").strip().lower()
    if emo not in EMOTIONS:
        emo = "neutral"
    now = time.time()
    with state_lock:
        receptionist["emotion"] = emo
        receptionist["emotion_history"].append({"ts": int(now * 1000), "emotion": emo})
        receptionist["last_seen_ts"] = now
    return jsonify({"ok": True})


@app.post("/reset")
def post_reset():
    """Clear all face enrollments and current visitor — for demo re-runs."""
    global current_visitor
    with state_lock:
        for p in PATIENTS.values():
            p["descriptors"] = []
    _last_auto_learn.clear()
    try:
        _db_clear_all()
    except sqlite3.Error as e:
        print(f"  face db: reset failed: {e}")
    current_visitor = None
    return jsonify({"ok": True})


# -------- Emotion routed to current visitor -----------------------------------

@app.post("/emotion")
def post_emotion():
    data = request.get_json(force=True)
    emo = (data.get("emotion") or "").strip().lower()
    if emo not in EMOTIONS:
        emo = "neutral"
    sent = write_serial(f"emotion:{emo}")
    pid = current_visitor
    if pid:
        with state_lock:
            v = vitals[pid]
            v["emotion"] = emo
            v["emotion_history"].append({"ts": int(time.time() * 1000), "emotion": emo})
    return jsonify({"ok": True, "sent_serial": sent, "current_visitor": pid})


# -------- Body language (arms crossed) routed to current visitor --------------

BODY_STATES = {"open", "crossed"}


@app.post("/body")
def post_body():
    """Debounced arms-crossed signal from the POV MediaPipe PoseLandmarker.

    Client sends transitions only (~0.5–1 Hz at most, not every frame), so we
    can safely store each as a history entry without extra throttling here.
    """
    data = request.get_json(force=True)
    state = (data.get("arms") or "").strip().lower()
    if state not in BODY_STATES:
        return jsonify({"ok": False, "error": "arms must be 'open' or 'crossed'"}), 400
    pid = current_visitor
    now = time.time()
    if pid:
        with state_lock:
            v = vitals[pid]
            v["arms_state"] = state
            v["arms_history"].append({"ts": int(now * 1000), "state": state})
            if state == "crossed":
                v["arms_ts"] = now
    return jsonify({"ok": True, "current_visitor": pid, "arms": state})


# -------- Claude triage coach -------------------------------------------------

TRIAGE_SYSTEM = (
    "You are a triage coach at an urgent-care reception desk. You observe the "
    "entire waiting room through vitals, wearables, and camera, AND the "
    "receptionist's own emotional state via a second camera. Your job is to "
    "tell the receptionist what to focus on right now — both for patients AND "
    "for their own wellbeing.\n\n"
    "OUTPUT STRICTLY 1 to 4 bullet lines, one per line, EXACTLY in the form:\n"
    "- [HIGH] Subject Name: action (<= 15 words)\n"
    "- [MED] Subject Name: action (<= 15 words)\n"
    "- [LOW] Subject Name: action (<= 15 words)\n\n"
    "Subject Name is either a patient's name OR the literal word Receptionist.\n\n"
    "Priority rubric:\n"
    "  HIGH  -> medical urgency (chest pain, SOB, abnormal vitals) OR acute "
    "patient crisis\n"
    "  MED   -> anxiety elevated, moderate wait, receptionist sustained distress, "
    "non-critical complaint\n"
    "  LOW   -> routine, calm, recent check-in\n\n"
    "Include a Receptionist action when their distress is sustained, session is "
    "long, or a simple patient-facing gesture would reduce everyone's stress "
    "(e.g., 'offer water to David', 'brief break — 20 min sustained focus').\n\n"
    "Body-language signal: 'body: arms crossed' in a patient row means the POV "
    "camera observed sustained crossed-arm posture — a mild guarding/discomfort "
    "cue. Don't over-weight it on its own, but combine with emotion + wait "
    "time to justify a check-in when present.\n\n"
    "Be concrete. No preamble, no summary, no extra text."
)


def build_triage_context() -> str:
    lines = ["PATIENTS:"]
    for pid, p in PATIENTS.items():
        snap = patient_snapshot(pid)
        wait_min = (snap["wait_s"] / 60.0) if snap["wait_s"] else 0
        anx_lbl = snap["anxiety"]["label"] if snap["anxiety"] else "unknown"
        anx_metric = snap["anxiety"]["metric"] if snap["anxiety"] else "n/a"
        hr_s = f"{snap['hr']} bpm" if snap["hr"] else "no signal"
        emo_s = snap["emotion"] or "unknown"
        loc = snap["bay"]
        at_counter = " (AT COUNTER)" if pid == current_visitor else ""
        body_flag = " | body: arms crossed" if snap.get("arms_active") else ""
        lines.append(
            f"- {snap['name']}, {snap['age']}{at_counter}: \"{snap['chief_complaint']}\" | "
            f"{loc}, waiting {wait_min:.1f} min | HR {hr_s} | anxiety {anx_lbl} "
            f"({anx_metric}) | distress {snap['distress']:.2f} | emotion {emo_s}{body_flag}"
        )
    r = receptionist_snapshot()
    lines.append("")
    lines.append("RECEPTIONIST:")
    if r["present"]:
        lines.append(
            f"- emotion {r['emotion'] or 'unknown'} | distress {r['distress']:.2f} | "
            f"on desk {r['session_min']:.1f} min"
        )
    else:
        lines.append("- off-camera / not currently detected")
    return "\n".join(lines)


BULLET_RE = re.compile(
    r"^\s*[-•*]\s*\[(HIGH|MED(?:IUM)?|LOW)\]\s*(.+?):\s*(.+?)\s*$",
    re.IGNORECASE,
)


def parse_triage(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = BULLET_RE.match(line)
        if m:
            lvl = m.group(1).upper()
            if lvl.startswith("MED"): lvl = "MED"
            out.append({
                "priority": lvl,
                "patient": m.group(2).strip(),
                "action": m.group(3).strip(),
            })
    return out


@app.post("/triage")
def post_triage():
    roster = build_triage_context()
    t0 = time.time()
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=TRIAGE_SYSTEM,
            messages=[{"role": "user", "content": f"Waiting room right now:\n\n{roster}"}],
        )
        text = resp.content[0].text.strip()
    except Exception as e:
        text = f"(error: {e})"
    items = parse_triage(text)
    entry = {
        "ts": int(time.time() * 1000),
        "latency_ms": int((time.time() - t0) * 1000),
        "raw": text,
        "items": items,
        "roster_snapshot": roster,
    }
    triage_history.append(entry)
    return jsonify(entry)


# -------- Status endpoint -----------------------------------------------------

@app.get("/status")
def status():
    snaps = [patient_snapshot(pid) for pid in PATIENTS]
    # Aggregate waiting-room stats
    waits = [p["wait_s"] for p in snaps if p["wait_s"] is not None]
    urgent = sum(1 for p in snaps if p["anxiety"] and p["anxiety"]["label"] == "elevated")
    stats = {
        "count_waiting": len(waits),
        "avg_wait_s": round(sum(waits) / len(waits), 1) if waits else None,
        "max_wait_s": round(max(waits), 1) if waits else None,
        "count_elevated": urgent,
    }
    return jsonify({
        "now_ms": int(time.time() * 1000),
        "serial_open": ser is not None,
        "model": MODEL,
        "current_visitor": current_visitor,
        "badges": [badge_snapshot(bid) for bid in badge_assignments],
        "patients": snaps,
        "stats": stats,
        "triage": list(triage_history)[-5:],
        "receptionist": receptionist_snapshot(),
        "session_s": round(time.time() - SESSION_START, 1),
    })


@app.get("/<pid>/nps")
def get_nps(pid: str):
    if pid not in PATIENTS:
        return jsonify({"error": "unknown patient", "id": pid}), 404
    return jsonify(nps_payload(pid))


@app.get("/badge/<badge_id>/nps")
def get_badge_nps(badge_id: str):
    if badge_id not in badge_assignments:
        return jsonify({"error": "unknown badge", "id": badge_id}), 404
    snap = badge_snapshot(badge_id)
    if not snap["patient_id"]:
        return jsonify({
            "badge_id": badge_id,
            "assigned_patient_id": None,
            "name": "badge idle",
            "score": 3,
            "distress": 0.0,
            "anxiety": None,
            "intervention": False,
        })
    payload = nps_payload(snap["patient_id"])
    payload["badge_id"] = badge_id
    payload["assigned_patient_id"] = snap["patient_id"]
    return jsonify(payload)


@app.post("/badge/<badge_id>/assign")
def post_badge_assign(badge_id: str):
    if badge_id not in badge_assignments:
        return jsonify({"ok": False, "error": "unknown badge"}), 404
    data = request.get_json(force=True) if request.data else {}
    patient_id = data.get("patient_id")
    if patient_id is not None and patient_id not in PATIENTS:
        return jsonify({"ok": False, "error": "unknown patient"}), 400
    with state_lock:
        badge_assignments[badge_id] = patient_id
    return jsonify({"ok": True, "badge": badge_snapshot(badge_id)})


@app.get("/")
def index():
    return Response(HTML, mimetype="text/html")


@app.get("/patient/<pid>")
def patient_page(pid: str):
    if pid not in PATIENTS:
        return Response("<h1 style='font-family:monospace;color:#fff;background:#0b0d10;padding:40px;'>404 · unknown patient</h1>",
                        status=404, mimetype="text/html")
    return Response(PATIENT_HTML, mimetype="text/html")


# -------- HTML ----------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>HappyClinic · reception triage</title>
<script defer src="https://cdn.jsdelivr.net/npm/face-api.js@0.22.2/dist/face-api.min.js"></script>
<script src="https://unpkg.com/three@0.149.0/build/three.min.js"></script>
<script type="module">
  // MediaPipe Tasks Vision — powers emotion (FaceLandmarker blendshapes) and
  // crossed-arms detection (PoseLandmarker). face-api.js stays loaded for
  // faceRecognitionNet descriptors only (MediaPipe has no identity embedding).
  import { FaceLandmarker, PoseLandmarker, FilesetResolver }
    from "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.34/vision_bundle.mjs";
  window.__mp = { FaceLandmarker, PoseLandmarker, FilesetResolver };
  window.dispatchEvent(new CustomEvent("mp-ready"));
</script>
<style>
  :root {
    --bg: #0b0d10; --panel: #14181d; --panel2:#191e25; --line: #222931;
    --text: #e6edf3; --mute: #7a8691; --faint: #4c5560;
    --ok: #7ee787; --warn: #f0883e; --err: #ff7b72; --claude: #d2a8ff; --hr: #ff5d73;
    --hi: #ff7b72; --md: #f0883e; --lo: #7ee787;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--text);
    font: 13px/1.4 ui-monospace, SF Mono, Menlo, Consolas, monospace;
    display: grid; grid-template-rows: auto 1fr;
    height: 100vh; overflow: hidden;
  }
  header {
    padding: 10px 20px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; gap: 24px;
  }
  header h1 { margin: 0; font-size: 13px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; }
  header h1 .thesis { color: var(--claude); font-weight: 400; margin-left: 6px; }
  header h1 .thesis .x { color: var(--mute); margin: 0 4px; }

  .column-header {
    font-size: 11px; font-weight: 700; letter-spacing: 0.15em; text-transform: uppercase;
    color: var(--text);
    padding-bottom: 10px; border-bottom: 2px solid var(--line);
    margin: 0; display: flex; align-items: baseline; gap: 10px; flex-shrink: 0;
  }
  .column-header .tag { color: var(--claude); font-size: 10px; font-weight: 400; letter-spacing: 0.08em; text-transform: none; }

  .step {
    display: inline-flex; align-items: center; gap: 8px;
  }
  .step .num {
    display: inline-flex; align-items: center; justify-content: center;
    width: 18px; height: 18px; border-radius: 50%;
    background: var(--claude); color: #0b0d10;
    font-size: 10px; font-weight: 700;
    flex-shrink: 0;
  }
  header .brand-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--claude); margin-right: 10px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.5;} }
  header .meta { color: var(--mute); font-size: 12px; display: flex; gap: 20px; margin-left: auto; }
  header .meta b { color: var(--text); font-weight: 600; }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--faint); margin-right: 5px; vertical-align: middle; }
  .dot.on { background: var(--ok); box-shadow: 0 0 6px var(--ok); }
  .dot.off { background: var(--err); }

  main { display: grid; grid-template-columns: minmax(340px, 420px) 1fr; min-height: 0; overflow: hidden; background: var(--line); gap: 1px; }
  section { background: var(--bg); padding: 14px 16px; overflow: auto; min-height: 0; display: flex; flex-direction: column; gap: 14px; }
  section h2 { margin: 0 0 8px; font-size: 10px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: var(--mute); display: flex; justify-content: space-between; align-items: baseline; }

  /* Left: camera + current visitor */
  .left-col { display: flex; flex-direction: column; gap: 12px; }
  .video-wrap { position: relative; aspect-ratio: 4/3; background: #000; border: 1px solid var(--line); overflow: hidden; }
  video { width: 100%; height: 100%; object-fit: cover; transform: scaleX(-1); }
  #overlay { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; transform: scaleX(-1); }
  .fps-tag { position: absolute; top: 8px; left: 10px; background: rgba(0,0,0,0.6); padding: 3px 7px; font-size: 10px; color: var(--ok); border-radius: 2px; }
  .enroll-tag { position: absolute; top: 8px; right: 10px; background: rgba(0,0,0,0.7); padding: 3px 8px; font-size: 10px; color: var(--claude); border-radius: 2px; display: none; }
  .enroll-tag.show { display: inline-block; }

  .visitor-card { border: 1px solid var(--line); background: var(--panel); padding: 12px; }
  .visitor-card.unknown { border-color: var(--warn); }
  .visitor-head { display: flex; justify-content: space-between; align-items: baseline; }
  .visitor-name { font-size: 18px; font-weight: 600; letter-spacing: 0.02em; }
  .visitor-age { color: var(--mute); font-size: 12px; }
  .visitor-complaint { color: var(--text); font-size: 12px; margin: 6px 0; font-style: italic; }
  .visitor-sub { display: flex; gap: 14px; color: var(--mute); font-size: 11px; margin-top: 6px; }
  .visitor-sub b { color: var(--text); font-weight: 500; }

  .ehr { border: 1px solid var(--line); background: var(--panel2); padding: 10px 12px; font-size: 11px; }
  .ehr-row { display: grid; grid-template-columns: 90px 1fr; gap: 8px; padding: 3px 0; color: var(--mute); }
  .ehr-row b { color: var(--text); font-weight: 500; font-size: 11px; }
  .ehr-title { color: var(--claude); font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 6px; }

  .vitals-strip { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .vitals-cell { border: 1px solid var(--line); background: var(--panel); padding: 10px; }
  .vitals-cell .label { font-size: 9px; color: var(--mute); letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 4px; }
  .vitals-cell .big { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1; }
  .vitals-cell .small { font-size: 11px; color: var(--mute); margin-top: 4px; }
  .vitals-cell.hr .big { color: var(--hr); }
  .vitals-cell .pill { display: inline-block; padding: 2px 7px; font-size: 10px; border-radius: 10px; letter-spacing: 0.05em; text-transform: uppercase; font-weight: 600; }
  .pill.calm { background: var(--ok); color: #0b0d10; }
  .pill.balanced { background: var(--warn); color: #0b0d10; }
  .pill.elevated { background: var(--err); color: #fff; }
  .pill.unknown { border: 1px solid var(--line); color: var(--mute); }
  .pill.badge { background: rgba(210,168,255,0.14); color: var(--claude); border: 1px solid rgba(210,168,255,0.35); }

  /* Right: triage hero + queue */
  .right-col { display: grid; grid-template-rows: auto 1fr auto; min-height: 0; gap: 12px; }

  .triage-hero {
    background: linear-gradient(180deg, rgba(210,168,255,0.05), transparent 70%), var(--panel);
    border: 1px solid var(--line);
    padding: 14px 16px;
  }
  .triage-hero-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }
  .triage-hero-title { font-size: 10px; color: var(--mute); letter-spacing: 0.1em; text-transform: uppercase; }
  .triage-hero-title b { color: var(--claude); font-weight: 600; }
  .triage-hero-meta { color: var(--mute); font-size: 11px; }
  .triage-hero-meta .next { color: var(--claude); }

  .triage-items { display: flex; flex-direction: column; gap: 8px; }
  .triage-item {
    display: grid; grid-template-columns: 60px 1fr; gap: 12px;
    padding: 10px; border-left: 3px solid var(--faint); background: rgba(255,255,255,0.02);
    align-items: baseline;
  }
  .triage-item.HIGH { border-left-color: var(--hi); background: rgba(255,123,114,0.08); }
  .triage-item.MED  { border-left-color: var(--md); background: rgba(240,136,62,0.05); }
  .triage-item.LOW  { border-left-color: var(--lo); background: rgba(126,231,135,0.03); }
  .triage-item .pri {
    font-weight: 700; font-size: 10px; letter-spacing: 0.1em;
    padding: 3px 7px; text-align: center; border-radius: 2px;
  }
  .triage-item.HIGH .pri { background: var(--hi); color: #fff; }
  .triage-item.MED  .pri { background: var(--md); color: #0b0d10; }
  .triage-item.LOW  .pri { background: var(--lo); color: #0b0d10; }
  .triage-item .body { font-size: 13px; line-height: 1.4; }
  .triage-item .body b { color: var(--text); }
  .triage-item .body .action { color: var(--text); font-family: ui-serif, Georgia, serif; font-size: 14px; }
  .triage-empty { color: var(--mute); font-size: 12px; padding: 10px 0; text-align: center; }

  .triage-controls { display: flex; gap: 10px; margin-top: 10px; align-items: center; font-size: 11px; color: var(--mute); }
  button { background: transparent; color: var(--text); border: 1px solid var(--line); padding: 5px 12px; font: inherit; font-size: 11px; cursor: pointer; border-radius: 2px; }
  button:hover { border-color: var(--claude); color: var(--claude); }
  input[type=number] { background: var(--panel); color: var(--text); border: 1px solid var(--line); padding: 3px 5px; width: 50px; font: inherit; font-size: 11px; }
  input[type=checkbox] { accent-color: var(--claude); }

  .triage-history { background: var(--panel2); border: 1px solid var(--line); padding: 10px 12px; overflow-y: auto; min-height: 0; }
  .triage-history-row { display: grid; grid-template-columns: 70px 1fr; gap: 10px; padding: 5px 0; border-bottom: 1px solid var(--line); font-size: 11px; }
  .triage-history-row:last-child { border-bottom: none; }
  .triage-history-row .t { color: var(--mute); font-variant-numeric: tabular-nums; }
  .triage-history-row .items { color: var(--mute); }
  .triage-history-row .items .mini { display: inline-block; margin-right: 6px; padding: 1px 6px; border-radius: 2px; font-size: 9px; font-weight: 600; letter-spacing: 0.08em; }
  .mini.HIGH { background: var(--hi); color: #fff; }
  .mini.MED  { background: var(--md); color: #0b0d10; }
  .mini.LOW  { background: var(--lo); color: #0b0d10; }

  /* 3D waiting room */
  .waiting-3d {
    background: radial-gradient(ellipse at center, #1a1f26 0%, #0b0d10 100%);
    border: 1px solid var(--line);
    height: 320px; min-height: 320px;
    position: relative; overflow: hidden;
  }
  .waiting-3d canvas { display: block; }
  .scene-legend {
    position: absolute; bottom: 8px; left: 10px;
    display: flex; gap: 12px; font-size: 9px; color: var(--mute);
    letter-spacing: 0.08em; text-transform: uppercase;
    background: rgba(0,0,0,0.5); padding: 4px 8px; border-radius: 2px;
  }
  .scene-legend span { display: flex; align-items: center; gap: 4px; }
  .scene-legend i { display: inline-block; width: 8px; height: 8px; border-radius: 50%; }
  .scene-hint { position: absolute; top: 8px; right: 10px; font-size: 9px; color: var(--mute); letter-spacing: 0.08em; text-transform: uppercase; background: rgba(0,0,0,0.4); padding: 3px 7px; border-radius: 2px; }

  /* Legacy 2D floor plan (kept for reference; unused) */
  .floor-plan {
    display: grid; grid-template-columns: 1fr 1fr 1fr;
    grid-template-rows: minmax(110px, 1fr) minmax(90px, auto);
    gap: 8px; height: 100%; min-height: 240px;
    background: linear-gradient(180deg, rgba(210,168,255,0.03), transparent), var(--panel2);
    border: 1px solid var(--line); padding: 10px; position: relative;
  }
  .zone {
    border: 1px dashed var(--line); border-radius: 4px;
    position: relative; padding: 22px 8px 8px 8px;
    display: flex; flex-wrap: wrap; gap: 10px;
    align-content: flex-start; justify-content: center;
    transition: all 0.3s;
  }
  .zone.desk { grid-column: 1; grid-row: 1; background: rgba(210,168,255,0.06); border-color: var(--claude); border-style: solid; }
  .zone.bay1 { grid-column: 2; grid-row: 1; }
  .zone.bay2 { grid-column: 3; grid-row: 1; }
  .zone.wait { grid-column: 1 / 4; grid-row: 2; background: rgba(255,255,255,0.01); }
  .zone-label {
    position: absolute; top: 5px; left: 8px;
    font-size: 9px; color: var(--mute); letter-spacing: 0.12em; text-transform: uppercase;
  }
  .zone-empty {
    margin: auto; color: var(--faint); font-size: 11px; font-style: italic;
  }
  .avatar {
    display: flex; flex-direction: column; align-items: center;
    gap: 2px; width: 72px; cursor: default;
  }
  .avatar-ring {
    width: 44px; height: 44px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    background: var(--panel); border: 2px solid var(--faint);
    font-weight: 600; font-size: 14px; color: var(--text);
    transition: all 0.3s;
  }
  .avatar-ring.calm     { border-color: var(--ok);    box-shadow: 0 0 10px rgba(126,231,135,0.35); }
  .avatar-ring.balanced { border-color: var(--warn);  box-shadow: 0 0 10px rgba(240,136,62,0.35); }
  .avatar-ring.elevated { border-color: var(--err);   box-shadow: 0 0 12px rgba(255,123,114,0.55); animation: ring-pulse 1.4s infinite; }
  .avatar.at-counter .avatar-ring { border-color: var(--claude); box-shadow: 0 0 18px rgba(210,168,255,0.7); transform: scale(1.12); }
  @keyframes ring-pulse { 0%,100%{box-shadow:0 0 12px rgba(255,123,114,0.5);} 50%{box-shadow:0 0 22px rgba(255,123,114,0.95);} }
  .avatar-name { font-size: 10px; text-align: center; color: var(--text); max-width: 72px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .avatar-hr { font-size: 10px; color: var(--hr); font-variant-numeric: tabular-nums; }
  .avatar-hr .arrow { font-size: 9px; margin-left: 2px; }
  .avatar-wait { font-size: 9px; color: var(--mute); }
  .avatar-wait.amber { color: var(--warn); }
  .avatar-wait.red   { color: var(--err); }

  /* Claim panel — shown when unknown face is at counter */
  .claim-panel {
    border: 1px solid var(--warn);
    background: rgba(240,136,62,0.08);
    padding: 10px 12px; border-radius: 3px;
    animation: fadein 0.2s ease-out;
  }
  @keyframes fadein { from { opacity: 0; } to { opacity: 1; } }
  .claim-title { color: var(--warn); font-size: 11px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; display: flex; align-items: center; gap: 6px; }
  .claim-sub { color: var(--mute); font-size: 11px; margin: 4px 0 8px; }
  .claim-buttons { display: flex; flex-wrap: wrap; gap: 6px; }
  .claim-buttons button {
    background: var(--panel); color: var(--text); border: 1px solid var(--line);
    font-size: 11px; padding: 6px 10px;
  }
  .claim-buttons button:hover { border-color: var(--warn); color: var(--warn); background: rgba(240,136,62,0.1); }
  .claim-buttons button .meta { color: var(--mute); font-weight: 400; margin-left: 6px; }

  /* Camera role selectors */
  .cam-setup { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; font-size: 10px; color: var(--mute); }
  .cam-setup label { display: flex; flex-direction: column; gap: 3px; letter-spacing: 0.08em; text-transform: uppercase; }
  .cam-setup select { background: var(--panel); color: var(--text); border: 1px solid var(--line); padding: 4px 6px; font: inherit; font-size: 11px; border-radius: 2px; }

  /* Receptionist card */
  .recep-card {
    border: 1px solid var(--line); background: var(--panel);
    padding: 10px; display: grid; grid-template-columns: 128px 1fr; gap: 12px;
    align-items: start;
  }
  .recep-video-wrap {
    position: relative; width: 128px; height: 96px;
    background: #000; border: 1px solid var(--line); overflow: hidden;
  }
  #video-recep { width: 100%; height: 100%; object-fit: cover; transform: scaleX(-1); }
  .recep-tag { position: absolute; top: 3px; left: 4px; font-size: 9px; color: var(--claude); background: rgba(0,0,0,0.6); padding: 2px 5px; border-radius: 2px; letter-spacing: 0.08em; text-transform: uppercase; }
  .recep-body { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .recep-head { display: flex; justify-content: space-between; align-items: baseline; }
  .recep-emotion { font-size: 24px; line-height: 1; }
  .recep-label { font-size: 11px; color: var(--text); font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; }
  .recep-sub { font-size: 10px; color: var(--mute); }
  .distress-bar { height: 6px; background: var(--bg); border: 1px solid var(--line); border-radius: 3px; overflow: hidden; margin: 4px 0; }
  .distress-bar .fill { height: 100%; background: linear-gradient(90deg, var(--ok), var(--warn), var(--err)); width: 0%; transition: width 0.3s; }
  .recep-flag { font-size: 10px; color: var(--warn); font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; }
  .recep-flag.ok { color: var(--ok); }

  /* Big receptionist video */
  .recep-video-big { position: relative; aspect-ratio: 4/3; background: #000; border: 1px solid var(--line); overflow: hidden; }
  #video-recep-big { width: 100%; height: 100%; object-fit: cover; transform: scaleX(-1); }
  .recep-overlay { position: absolute; top: 8px; left: 10px; background: rgba(0,0,0,0.55); padding: 4px 8px; font-size: 10px; color: var(--claude); border-radius: 2px; letter-spacing: 0.1em; text-transform: uppercase; }
  .recep-pill-overlay { position: absolute; bottom: 8px; left: 10px; background: rgba(0,0,0,0.6); padding: 4px 10px; font-size: 11px; border-radius: 12px; }

  /* Stats row */
  .stats-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .stat-tile { border: 1px solid var(--line); background: var(--panel); padding: 10px 12px; display: flex; flex-direction: column; gap: 2px; }
  .stat-tile .stat-label { font-size: 9px; color: var(--mute); letter-spacing: 0.1em; text-transform: uppercase; }
  .stat-tile .stat-value { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.1; }
  .stat-tile.urgent .stat-value { color: var(--err); }
  .stat-tile.waiting .stat-value { color: var(--claude); }
  .stat-tile .stat-sub { font-size: 10px; color: var(--mute); }

  /* Patient accordion list */
  .patient-list { display: flex; flex-direction: column; gap: 6px; }
  details.patient-item {
    background: var(--panel); border: 1px solid var(--line);
    border-left: 3px solid var(--faint); border-radius: 2px;
    transition: border-color 0.2s;
  }
  details.patient-item.calm     { border-left-color: var(--ok); }
  details.patient-item.balanced { border-left-color: var(--warn); }
  details.patient-item.elevated { border-left-color: var(--err); }
  details.patient-item[open]    { background: var(--panel2); border-left-color: var(--claude); }
  details.patient-item summary {
    padding: 9px 14px; cursor: pointer;
    display: grid; grid-template-columns: 1fr auto auto auto; gap: 10px; align-items: center;
    list-style: none; user-select: none;
  }
  details.patient-item summary::-webkit-details-marker { display: none; }
  details.patient-item summary::marker { display: none; }
  details.patient-item summary::before {
    content: "▸"; display: inline-block; margin-right: 4px; color: var(--mute); transition: transform 0.15s;
  }
  details.patient-item[open] summary::before { transform: rotate(90deg); }
  .pi-name { display: inline-flex; align-items: baseline; gap: 6px; }
  .pi-name b { font-size: 13px; font-weight: 600; }
  .pi-name .age { color: var(--mute); font-size: 11px; }
  .pi-name .at { color: var(--claude); font-size: 10px; letter-spacing: 0.1em; margin-left: 6px; }
  .pi-meta { display: flex; gap: 10px; align-items: center; font-size: 11px; color: var(--mute); }
  .pi-meta .hr { color: var(--hr); font-variant-numeric: tabular-nums; }
  .pi-meta .bay { font-size: 10px; letter-spacing: 0.05em; text-transform: uppercase; }
  .pi-wait {
    font-size: 10px; font-variant-numeric: tabular-nums; letter-spacing: 0.05em;
    padding: 2px 7px; border-radius: 2px; background: rgba(255,255,255,0.04); color: var(--text);
  }
  .pi-open {
    color: var(--claude); text-decoration: none; font-size: 11px;
    padding: 3px 8px; border: 1px solid var(--line); border-radius: 2px;
    letter-spacing: 0.05em; margin-left: 6px;
  }
  .pi-open:hover { border-color: var(--claude); background: rgba(210,168,255,0.08); }
  .pi-wait.green { background: rgba(126,231,135,0.18); color: var(--ok); }
  .pi-wait.amber { background: rgba(240,136,62,0.18); color: var(--warn); }
  .pi-wait.red   { background: rgba(255,123,114,0.22); color: var(--err); }
  .pi-body {
    padding: 12px 16px 14px; border-top: 1px solid var(--line);
    display: grid; gap: 8px; font-size: 12px;
  }
  .pi-actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .badge-assign-btn.assigned { border-color: var(--claude); color: var(--claude); }
  .badge-clear-btn:hover { border-color: var(--err); color: var(--err); }
  .pi-body .complaint { font-style: italic; color: var(--text); }
  .pi-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 14px; color: var(--mute); font-size: 11px; }
  .pi-grid b { color: var(--text); font-weight: 500; }
  .pi-vitals-row { display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px; }
  .pi-vitals-row .chip { background: var(--bg); border: 1px solid var(--line); padding: 3px 7px; border-radius: 10px; color: var(--mute); }
  .pi-vitals-row .chip b { color: var(--text); font-weight: 500; margin-left: 4px; }

  /* Visual row: 3D scene + POV camera side-by-side */
  .visual-row { display: grid; grid-template-columns: minmax(0, 2fr) minmax(260px, 1fr); gap: 1px; background: var(--line); }
  .visual-row > div { background: var(--bg); padding: 0 0 0 0; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
  .visual-row > div > h2 { margin: 0; }
  .pov-stacked { display: flex; flex-direction: column; gap: 8px; }
  .pov-stacked .video-wrap { aspect-ratio: 4/3; width: 100%; }
  .pov-stacked .cam-setup select { width: 100%; }
  .pov-meta-row { display: flex; gap: 6px; align-items: center; font-size: 10px; color: var(--mute); }
  .arms-pill { padding: 2px 8px; border-radius: 10px; border: 1px solid var(--line); font-variant-numeric: tabular-nums; }
  .arms-pill.crossed { background: rgba(255,123,114,0.15); border-color: var(--err); color: var(--err); }
  .arms-pill.open    { color: var(--mute); }
  .arms-pill.unknown { color: var(--faint); }
  .pi-vitals-row .chip.arms-chip.crossed { background: rgba(255,123,114,0.1); border-color: var(--err); color: var(--err); }

  .queue { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 8px; }
  .patient-card {
    background: var(--panel); border: 1px solid var(--line);
    border-left: 3px solid var(--faint); padding: 10px;
    display: flex; flex-direction: column; gap: 6px;
    transition: border-color 0.3s;
  }
  .patient-card.at-counter { border-left-color: var(--claude); box-shadow: 0 0 0 1px rgba(210,168,255,0.3); }
  .patient-card .pc-head { display: flex; justify-content: space-between; align-items: baseline; }
  .patient-card .pc-name { font-size: 13px; font-weight: 600; }
  .patient-card .pc-wait {
    font-size: 10px; font-variant-numeric: tabular-nums; letter-spacing: 0.05em;
    padding: 2px 6px; border-radius: 2px; background: var(--faint); color: var(--text);
  }
  .patient-card .pc-wait.green { background: rgba(126,231,135,0.2); color: var(--ok); }
  .patient-card .pc-wait.amber { background: rgba(240,136,62,0.2); color: var(--warn); }
  .patient-card .pc-wait.red   { background: rgba(255,123,114,0.25); color: var(--err); }
  .patient-card .pc-complaint { color: var(--mute); font-size: 11px; font-style: italic; }
  .patient-card .pc-vitals { display: flex; gap: 10px; align-items: baseline; font-size: 11px; }
  .patient-card .pc-vitals b { color: var(--hr); font-size: 15px; font-variant-numeric: tabular-nums; }
  .patient-card .pc-bay { color: var(--mute); font-size: 10px; letter-spacing: 0.05em; text-transform: uppercase; }
  .patient-card canvas { width: 100%; height: 30px; display: block; margin-top: 4px; }

  @keyframes newitem { 0% { transform: scale(0.96); opacity: 0; } 100% { transform: scale(1); opacity: 1; } }
  .triage-item.new { animation: newitem 0.3s ease-out; }
</style>
</head>
<body>
<header>
  <h1><span class="brand-dot"></span>HappyClinic<span class="thesis">staff wellbeing<span class="x">×</span>patient triage</span></h1>
  <div class="meta">
    <span><span id="dot-cam" class="dot"></span>camera</span>
    <span><span id="dot-fa"  class="dot"></span>face-api</span>
    <span><span id="dot-esp" class="dot"></span>esp32</span>
    <span><span id="dot-hr"  class="dot"></span>garmin</span>
    <span>patients waiting <b id="n-waiting">—</b></span>
    <span>model <b id="model-name">—</b></span>
    <span id="clock">—</span>
    <button id="reset-enroll" title="clear all face enrollments">reset enrollment</button>
  </div>
</header>

<main>
  <!-- LEFT: Receptionist focus -->
  <section class="left-col">
    <div class="column-header">Receptionist Focus <span class="tag">staff-side — the part everyone else ignores</span></div>

    <div>
      <h2>live staff camera</h2>
      <div class="recep-video-big">
        <video id="video-recep-big" autoplay playsinline muted></video>
        <div class="recep-overlay">staff camera</div>
        <div class="recep-pill-overlay"><span class="pill unknown" id="recep-pill">—</span></div>
      </div>
    </div>

    <div class="recep-card" style="grid-template-columns: auto 1fr;">
      <div style="font-size:36px" id="recep-emoji">—</div>
      <div class="recep-body">
        <div class="recep-head">
          <span class="recep-label" id="recep-label">off-camera</span>
        </div>
        <div class="recep-sub" id="recep-session">session —</div>
        <div class="distress-bar"><div class="fill" id="recep-bar"></div></div>
        <div class="recep-sub"><span id="recep-distress">distress —</span></div>
        <div class="recep-flag" id="recep-flag"></div>
      </div>
    </div>

    <!-- Hidden (legacy): kept for JS hooks that still read these -->
    <video id="video-recep" autoplay playsinline muted style="display:none;"></video>

    <div class="triage-hero">
      <div class="triage-hero-head">
        <div class="triage-hero-title"><b>claude triage coach</b> · sonnet 4.6</div>
        <div class="triage-hero-meta">
          <span id="last-triage-time">—</span> · <span class="next" id="next-triage">next in —</span>
        </div>
      </div>
      <div class="triage-items" id="triage-items">
        <div class="triage-empty">first assessment fires shortly…</div>
      </div>
      <div class="triage-controls">
        <button id="triage-now">run triage now</button>
        <label>every <input id="triage-sec" type="number" min="10" max="300" step="5" value="30" /> s</label>
        <label style="margin-left:auto"><input id="voice" type="checkbox" /> speak HIGH alerts</label>
      </div>
    </div>
  </section>

  <!-- RIGHT: Waiting room + patients + POV -->
  <section class="right-col">
    <div class="column-header">Waiting Room Operations <span class="tag">patient-side — sensed, triaged, prioritized</span></div>

    <div>
      <h2 class="step"><span class="num">1</span> stats · at a glance</h2>
      <div class="stats-row">
        <div class="stat-tile waiting">
          <div class="stat-label">patients waiting</div>
          <div class="stat-value" id="stat-waiting">—</div>
          <div class="stat-sub" id="stat-waiting-sub">—</div>
        </div>
        <div class="stat-tile">
          <div class="stat-label">average wait</div>
          <div class="stat-value" id="stat-avg">—</div>
          <div class="stat-sub" id="stat-max-sub">—</div>
        </div>
        <div class="stat-tile urgent">
          <div class="stat-label">elevated anxiety</div>
          <div class="stat-value" id="stat-urgent">—</div>
          <div class="stat-sub">needs priority attention</div>
        </div>
      </div>
    </div>

    <div class="visual-row">
      <div>
        <h2 class="step"><span class="num">2</span> 3D waiting room <span style="color:var(--mute);text-transform:none;letter-spacing:0;margin-left:6px;">· rings = anxiety · pulse = HR</span></h2>
        <div class="waiting-3d" id="waiting-3d">
          <div class="scene-hint">live</div>
          <div class="scene-legend">
            <span><i style="background:#7ee787"></i>calm</span>
            <span><i style="background:#f0883e"></i>balanced</span>
            <span><i style="background:#ff7b72"></i>elevated</span>
            <span><i style="background:#d2a8ff"></i>at counter</span>
          </div>
        </div>
      </div>

      <div>
        <h2 class="step"><span class="num">3</span> POV camera <span style="color:var(--mute);text-transform:none;letter-spacing:0;margin-left:6px;">· iPhone recognizes who's at counter</span></h2>
        <div class="pov-stacked">
          <div class="video-wrap">
            <video id="video" autoplay playsinline muted></video>
            <canvas id="overlay"></canvas>
            <div class="fps-tag" id="fps">—</div>
            <div class="enroll-tag" id="enroll-tag">enrolled</div>
          </div>
          <div class="cam-setup">
            <label>POV<select id="cam-pov"></select></label>
            <label>Staff<select id="cam-recep"></select></label>
          </div>
          <div class="pov-meta-row">
            <button id="cam-refresh" style="font-size:10px;padding:3px 8px;">↻ re-scan</button>
            <span id="cam-count">—</span>
            <span id="arms-pill" class="arms-pill unknown" title="MediaPipe PoseLandmarker · crossed arms detected when both wrists cross the torso midline">arms: —</span>
          </div>
          <div class="claim-panel" id="claim-panel" style="display:none;">
            <div class="claim-title">⚠ unknown at counter</div>
            <div class="claim-sub">assign to:</div>
            <div class="claim-buttons" id="claim-buttons"></div>
          </div>
        </div>
      </div>
    </div>

    <div>
      <h2 class="step"><span class="num">4</span> patient records <span style="color:var(--mute);text-transform:none;letter-spacing:0;margin-left:6px;">· click a row for full EHR</span></h2>
      <div class="patient-list" id="patient-list"></div>
    </div>
  </section>
</main>

<audio id="chime" preload="auto">
  <source src="data:audio/wav;base64,UklGRlYEAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YTIEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" />
</audio>

<script>
// --- Refs ---
const video = document.getElementById("video");
const overlay = document.getElementById("overlay");
const octx = overlay.getContext("2d");
const fpsEl = document.getElementById("fps");
const enrollTag = document.getElementById("enroll-tag");
const dotCam = document.getElementById("dot-cam");
const dotFa  = document.getElementById("dot-fa");
const dotEsp = document.getElementById("dot-esp");
const dotHr  = document.getElementById("dot-hr");
const clockEl = document.getElementById("clock");
const modelName = document.getElementById("model-name");
const nWaiting = document.getElementById("n-waiting");

const visitorCard = document.getElementById("visitor-card");
const visitorName = document.getElementById("visitor-name");
const visitorAge = document.getElementById("visitor-age");
const visitorStatus = document.getElementById("visitor-status");
const visitorComplaint = document.getElementById("visitor-complaint");
const visitorBay = document.getElementById("visitor-bay");
const visitorWait = document.getElementById("visitor-wait");
const ehrAllergies = document.getElementById("ehr-allergies");
const ehrMeds = document.getElementById("ehr-meds");
const ehrLast = document.getElementById("ehr-last");

const vHr = document.getElementById("v-hr");
const vHrSub = document.getElementById("v-hr-sub");
const vAnx = document.getElementById("v-anx");
const vAnxSub = document.getElementById("v-anx-sub");
const vDistress = document.getElementById("v-distress");
const vEmoji = document.getElementById("v-emoji");
const vEmotion = document.getElementById("v-emotion");

const triageItems = document.getElementById("triage-items");
const lastTriageTime = document.getElementById("last-triage-time");
const nextTriage = document.getElementById("next-triage");
const triageNowBtn = document.getElementById("triage-now");
const triageSec = document.getElementById("triage-sec");
const voiceChk = document.getElementById("voice");

const claimPanel = document.getElementById("claim-panel");
const claimButtons = document.getElementById("claim-buttons");
const resetBtn = document.getElementById("reset-enroll");

const videoRecep = document.getElementById("video-recep-big");
const camPovSel = document.getElementById("cam-pov");
const camRecepSel = document.getElementById("cam-recep");
const armsPill = document.getElementById("arms-pill");
const recepEmoji = document.getElementById("recep-emoji");
const recepLabel = document.getElementById("recep-label");
const recepSession = document.getElementById("recep-session");
const recepBar = document.getElementById("recep-bar");
const recepDistress = document.getElementById("recep-distress");
const recepFlag = document.getElementById("recep-flag");
const recepPill = document.getElementById("recep-pill");
const statWaiting = document.getElementById("stat-waiting");
const statWaitingSub = document.getElementById("stat-waiting-sub");
const statAvg = document.getElementById("stat-avg");
const statMaxSub = document.getElementById("stat-max-sub");
const statUrgent = document.getElementById("stat-urgent");
const patientListEl = document.getElementById("patient-list");

const EMOJIS = { happy:"😊", sad:"😢", angry:"😠", surprised:"😮", neutral:"😐" };
const EMO_COLORS = { happy:"#7ee787", sad:"#79c0ff", angry:"#ff7b72", surprised:"#f0883e", neutral:"#6e7781" };

let ready = false;
let faceMatchState = { patientId: null, distance: null };
let lastSentEmotion = null;
let lastDescriptor = null;         // most recent face descriptor (for manual claim)
let lastDescriptorAt = 0;
let lastFaceDetectedAt = 0;        // for claim-panel throttling
let nextTriageAt = 0;
let lastTriageIds = new Set();
let frameCount = 0, lastFpsCount = 0, lastFpsAt = performance.now();
let badges = [];
let primaryBadgeId = "badge-1";

// --- Camera + face-api ---
let povStream = null, recepStream = null;

async function listCameras() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  const vids = devices.filter(d => d.kind === "videoinput");
  const povPrev = camPovSel.value, recepPrev = camRecepSel.value;
  for (const sel of [camPovSel, camRecepSel]) {
    sel.innerHTML = "";
    for (const v of vids) {
      const opt = document.createElement("option");
      opt.value = v.deviceId;
      opt.textContent = v.label || "(unknown camera)";
      sel.appendChild(opt);
    }
  }
  const countEl = document.getElementById("cam-count");
  if (countEl) countEl.textContent = `${vids.length} camera${vids.length !== 1 ? "s" : ""} found`;
  // Auto-assign: iPhone/Continuity -> POV, FaceTime/built-in -> Receptionist
  const iphone = vids.find(v => /iphone|continuity/i.test(v.label));
  const builtin = vids.find(v => /facetime|built-?in/i.test(v.label));
  const saved = { pov: localStorage.getItem("cam-pov"), rec: localStorage.getItem("cam-recep") };
  camPovSel.value   = saved.pov && vids.some(v => v.deviceId === saved.pov) ? saved.pov
                    : iphone?.deviceId
                    || vids[0]?.deviceId || "";
  camRecepSel.value = saved.rec && vids.some(v => v.deviceId === saved.rec) ? saved.rec
                    : builtin?.deviceId
                    || vids.find(v => v.deviceId !== camPovSel.value)?.deviceId
                    || vids[0]?.deviceId || "";
  return vids;
}

async function attachCamera(videoEl, deviceId, currentStream) {
  if (currentStream) { currentStream.getTracks().forEach(t => t.stop()); }
  if (!deviceId) return null;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { deviceId: { exact: deviceId }, width: 640, height: 480 },
      audio: false,
    });
    videoEl.srcObject = stream;
    await new Promise(r => videoEl.onloadedmetadata = r);
    return stream;
  } catch (e) {
    console.warn("camera attach failed:", e);
    return null;
  }
}

async function startCamera() {
  try {
    // Gate permission (prompts the user), then enumerate labels
    const probe = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
    probe.getTracks().forEach(t => t.stop());
  } catch (e) {
    dotCam.classList.add("off");
    console.warn("camera permission denied:", e);
    return;
  }
  await listCameras();
  povStream = await attachCamera(video, camPovSel.value, null);
  recepStream = await attachCamera(videoRecep, camRecepSel.value, null);
  dotCam.classList.toggle("on", !!povStream);
  dotCam.classList.toggle("off", !povStream);
}

camPovSel.addEventListener("change", async () => {
  localStorage.setItem("cam-pov", camPovSel.value);
  povStream = await attachCamera(video, camPovSel.value, povStream);
});
camRecepSel.addEventListener("change", async () => {
  localStorage.setItem("cam-recep", camRecepSel.value);
  recepStream = await attachCamera(videoRecep, camRecepSel.value, recepStream);
});

document.getElementById("cam-refresh")?.addEventListener("click", async () => {
  // Request a one-shot stream with no deviceId to nudge the browser into
  // re-enumerating (Continuity Camera often appears only after this).
  try {
    const s = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
    s.getTracks().forEach(t => t.stop());
  } catch {}
  await listCameras();
});

// MediaPipe task handles (see scripted ESM loader at top of document).
const MP_WASM     = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.34/wasm";
const FACE_MODEL  = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task";
const POSE_MODEL  = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task";
let faceLandmarkerPov  = null;
let faceLandmarkerRecep = null;
let poseLandmarker     = null;

async function loadModels() {
  // face-api.js — kept only for faceRecognitionNet (128-D identity descriptors).
  // tinyFaceDetector + faceLandmark68Net are prerequisites for descriptor
  // extraction; faceExpressionNet is no longer loaded — emotion moved to MP.
  const FACE_API_MODELS = "https://justadudewhohacks.github.io/face-api.js/models";
  const faceApiP = Promise.all([
    faceapi.nets.tinyFaceDetector.loadFromUri(FACE_API_MODELS),
    faceapi.nets.faceLandmark68Net.loadFromUri(FACE_API_MODELS),
    faceapi.nets.faceRecognitionNet.loadFromUri(FACE_API_MODELS),
  ]);

  if (!window.__mp) {
    await new Promise(res => window.addEventListener("mp-ready", res, { once: true }));
  }
  const { FaceLandmarker, PoseLandmarker, FilesetResolver } = window.__mp;
  const vision = await FilesetResolver.forVisionTasks(MP_WASM);
  const faceOpts = {
    baseOptions: { modelAssetPath: FACE_MODEL, delegate: "GPU" },
    outputFaceBlendshapes: true,
    runningMode: "VIDEO",
    numFaces: 1,
  };
  // Separate FaceLandmarker instances per video source — VIDEO mode expects
  // monotonic timestamps per task, sharing across two videos can desync.
  [faceLandmarkerPov, faceLandmarkerRecep] = await Promise.all([
    FaceLandmarker.createFromOptions(vision, faceOpts),
    FaceLandmarker.createFromOptions(vision, faceOpts),
  ]);
  poseLandmarker = await PoseLandmarker.createFromOptions(vision, {
    baseOptions: { modelAssetPath: POSE_MODEL, delegate: "GPU" },
    runningMode: "VIDEO",
    numPoses: 1,
  });

  await faceApiP;
  dotFa.classList.add("on");
  ready = true;
}

function resizeOverlay() {
  const r = overlay.getBoundingClientRect();
  overlay.width = r.width * devicePixelRatio;
  overlay.height = r.height * devicePixelRatio;
}
window.addEventListener("resize", resizeOverlay);

function drawBbox(detection, label, sub) {
  const w = overlay.width, h = overlay.height;
  octx.clearRect(0, 0, w, h);
  if (!detection) return;
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return;
  const b = detection.box;
  const sx = w / vw, sy = h / vh;
  const x = b.x * sx, y = b.y * sy, bw = b.width * sx, bh = b.height * sy;
  const col = label ? "#d2a8ff" : "#f0883e";
  octx.lineWidth = 3;
  octx.strokeStyle = col;
  octx.shadowColor = col;
  octx.shadowBlur = 8;
  octx.strokeRect(x, y, bw, bh);
  octx.shadowBlur = 0;
  // Label on top-left, unmirrored
  octx.save();
  octx.translate(x + bw, y);
  octx.scale(-1, 1);
  const tag = (label || "unknown visitor") + (sub ? ` · ${sub}` : "");
  octx.font = `${12 * devicePixelRatio}px ui-monospace, SF Mono, Menlo, monospace`;
  const tw = octx.measureText(tag).width + 12 * devicePixelRatio;
  const th = 20 * devicePixelRatio;
  octx.fillStyle = "rgba(0,0,0,0.8)";
  octx.fillRect(0, -th, tw, th);
  octx.fillStyle = col;
  octx.fillText(tag, 6 * devicePixelRatio, -6 * devicePixelRatio);
  octx.restore();
}

// Blendshape -> emotion scorer. Ported from ~/projects/nano_hackathon/web/app.js
// (MediaPipe FaceLandmarker outputs 52 ARKit-style blendshape coefficients
// 0..1; these weighted sums are the hand-tuned mapping from that sketch).
function scoreEmotions(bs) {
  const v = (k) => bs[k] || 0;
  const happy = (v("mouthSmileLeft") + v("mouthSmileRight")) / 2
              + (v("cheekSquintLeft") + v("cheekSquintRight")) * 0.15;
  const sad   = (v("mouthFrownLeft") + v("mouthFrownRight")) / 2
              + v("browInnerUp") * 0.3;
  const angryRaw = (v("browDownLeft") + v("browDownRight")) / 2
                 + (v("noseSneerLeft") + v("noseSneerRight")) * 0.15;
  // Gate: both inner brows need to be meaningfully down, otherwise a neutral
  // face with mild asymmetry flickers into "angry".
  const angry = (v("browDownLeft") > 0.4 && v("browDownRight") > 0.4) ? angryRaw : angryRaw * 0.25;
  const surprised = (v("eyeWideLeft") + v("eyeWideRight")) / 2
                  + v("jawOpen") * 0.3
                  + (v("browOuterUpLeft") + v("browOuterUpRight")) * 0.15;
  const scores = { happy, sad, angry, surprised };
  const max = Math.max(...Object.values(scores));
  const threshold = 0.25;
  if (max <= threshold) return { label: "neutral", score: 1 - max };
  let best = "neutral", bestV = 0;
  for (const k of Object.keys(scores)) if (scores[k] > bestV) { bestV = scores[k]; best = k; }
  return { label: best, score: bestV };
}

function blendshapesFromResult(result) {
  if (!result || !result.faceBlendshapes || result.faceBlendshapes.length === 0) return null;
  const bs = {};
  for (const cat of result.faceBlendshapes[0].categories) bs[cat.categoryName] = cat.score;
  return bs;
}

function bboxFromLandmarks(lm, vw, vh) {
  if (!lm || !lm.length) return null;
  let xmin = 1, ymin = 1, xmax = 0, ymax = 0;
  for (const p of lm) {
    if (p.x < xmin) xmin = p.x;
    if (p.y < ymin) ymin = p.y;
    if (p.x > xmax) xmax = p.x;
    if (p.y > ymax) ymax = p.y;
  }
  return { x: xmin * vw, y: ymin * vh, width: (xmax - xmin) * vw, height: (ymax - ymin) * vh };
}

// MediaPipe Pose indices: 11 L-shoulder, 12 R-shoulder, 15 L-wrist, 16 R-wrist.
// "left" is the subject's own left (i.e. camera-right when facing the camera).
// Ported verbatim from nano_hackathon initial commit's detectArmsCrossed.
function detectArmsCrossed(landmarks) {
  if (!landmarks) return { crossed: false, confidence: 0 };
  const Ls = landmarks[11], Rs = landmarks[12], Lw = landmarks[15], Rw = landmarks[16];
  const minVis = Math.min(Ls?.visibility ?? 0, Rs?.visibility ?? 0, Lw?.visibility ?? 0, Rw?.visibility ?? 0);
  if (minVis < 0.5) return { crossed: false, confidence: 0 };
  const midX = (Ls.x + Rs.x) / 2;
  const shoulderWidth = Math.abs(Ls.x - Rs.x);
  const leftWristCrossed  = Lw.x < midX;    // subject-left wrist has moved past midline
  const rightWristCrossed = Rw.x > midX;
  const wristGap = Math.abs(Lw.x - Rw.x);
  // Close-together wrists gate: arms flung open with wrists on opposite sides
  // of the torso shouldn't register as "crossed".
  const crossed = leftWristCrossed && rightWristCrossed && wristGap < shoulderWidth * 1.2;
  return { crossed, confidence: minVis };
}

function updateArmsPill(state, conf) {
  if (!armsPill) return;
  armsPill.classList.remove("crossed", "open", "unknown");
  if (state === "crossed") {
    armsPill.classList.add("crossed");
    armsPill.textContent = `arms: crossed${conf ? " · " + conf.toFixed(2) : ""}`;
  } else if (state === "open") {
    armsPill.classList.add("open");
    armsPill.textContent = "arms: open";
  } else {
    armsPill.classList.add("unknown");
    armsPill.textContent = "arms: —";
  }
}

async function sendEmotion(label) {
  if (label === lastSentEmotion) return;
  lastSentEmotion = label;
  try {
    const r = await fetch("/emotion", {
      method: "POST", headers: {"content-type":"application/json"},
      body: JSON.stringify({ emotion: label }),
    });
    const j = await r.json();
    dotEsp.classList.toggle("on", !!j.sent_serial);
    dotEsp.classList.toggle("off", !j.sent_serial);
  } catch {}
}

let lastSentArms = null;
async function sendBody(arms, confidence) {
  if (arms === lastSentArms) return;
  lastSentArms = arms;
  try {
    await fetch("/body", {
      method: "POST", headers: {"content-type":"application/json"},
      body: JSON.stringify({ arms, confidence: confidence ?? null }),
    });
  } catch {}
}

async function sendDescriptor(arr) {
  try {
    const r = await fetch("/face", {
      method: "POST", headers: {"content-type":"application/json"},
      body: JSON.stringify({ descriptor: Array.from(arr) }),
    });
    const j = await r.json();
    faceMatchState = j.match
      ? { patientId: j.match.patient_id, distance: j.match.distance }
      : { patientId: null, distance: null };
  } catch {}
}

async function claimCurrentFace(pid) {
  if (!lastDescriptor) return;
  try {
    const r = await fetch("/claim", {
      method: "POST", headers: {"content-type":"application/json"},
      body: JSON.stringify({ patient_id: pid, descriptor: Array.from(lastDescriptor) }),
    });
    const j = await r.json();
    if (j.ok) {
      enrollTag.classList.add("show");
      enrollTag.textContent = "✓ claimed as " + j.name;
      setTimeout(() => enrollTag.classList.remove("show"), 2500);
      pollStatus();
    }
  } catch {}
}

async function assignBadge(patientId) {
  if (!primaryBadgeId) return;
  try {
    const r = await fetch(`/badge/${encodeURIComponent(primaryBadgeId)}/assign`, {
      method: "POST", headers: {"content-type":"application/json"},
      body: JSON.stringify({ patient_id: patientId }),
    });
    const j = await r.json();
    if (j.ok) pollStatus();
  } catch {}
}

async function clearBadge() {
  if (!primaryBadgeId) return;
  try {
    const r = await fetch(`/badge/${encodeURIComponent(primaryBadgeId)}/assign`, {
      method: "POST", headers: {"content-type":"application/json"},
      body: JSON.stringify({ patient_id: null }),
    });
    const j = await r.json();
    if (j.ok) pollStatus();
  } catch {}
}

async function resetEnrollments() {
  if (!confirm("Clear all face enrollments? Patient records remain; descriptors are wiped.")) return;
  try { await fetch("/reset", { method: "POST" }); pollStatus(); } catch {}
}

function updateClaimPanel(patients, current) {
  const hasRecentFace = Date.now() - lastFaceDetectedAt < 3500;
  const unenrolled = patients.filter(p => !p.enrolled);
  const show = hasRecentFace && !current && unenrolled.length > 0;
  if (!show) {
    claimPanel.style.display = "none";
    return;
  }
  claimPanel.style.display = "block";
  claimButtons.innerHTML = "";
  for (const p of unenrolled) {
    const b = document.createElement("button");
    b.innerHTML = `claim as ${escapeHtml(p.name)} <span class="meta">${escapeHtml(p.chief_complaint.slice(0, 30))}</span>`;
    b.addEventListener("click", () => claimCurrentFace(p.id));
    claimButtons.appendChild(b);
  }
}

let lastRecepTs = -1;
async function detectLoopReceptionist() {
  if (!ready || !faceLandmarkerRecep) { setTimeout(detectLoopReceptionist, 600); return; }
  if (!recepStream || videoRecep.readyState < 2) { setTimeout(detectLoopReceptionist, 800); return; }
  try {
    // MediaPipe VIDEO mode requires strictly monotonic timestamps per task;
    // currentTime jumps via seeking/reload can go backward, so clamp.
    let ts = Math.round((videoRecep.currentTime || 0) * 1000);
    if (ts <= lastRecepTs) ts = lastRecepTs + 1;
    lastRecepTs = ts;
    const result = faceLandmarkerRecep.detectForVideo(videoRecep, ts);
    const bs = blendshapesFromResult(result);
    if (bs) {
      const { label } = scoreEmotions(bs);
      fetch("/receptionist", {
        method: "POST", headers: {"content-type":"application/json"},
        body: JSON.stringify({ emotion: label }),
      }).catch(() => {});
    }
  } catch (e) { /* occasional MP transient on track change — swallow */ }
  setTimeout(detectLoopReceptionist, 800);  // ~1.25 Hz
}

function renderReceptionist(r) {
  if (!r) return;
  const setPill = (cls, txt) => { if (recepPill) { recepPill.className = "pill " + cls; recepPill.textContent = txt; } };
  if (!r.present) {
    recepLabel.textContent = "off-camera";
    recepEmoji.textContent = "—";
    recepSession.textContent = `session ${fmtWait(r.session_s)}`;
    recepBar.style.width = "0%";
    recepDistress.textContent = "distress —";
    recepFlag.textContent = "";
    recepFlag.className = "recep-flag";
    setPill("unknown", "off-camera");
    return;
  }
  recepLabel.textContent = r.emotion || "neutral";
  recepEmoji.textContent = EMOJIS[r.emotion] || "—";
  recepSession.textContent = `on desk ${r.session_min.toFixed(1)} min`;
  const pct = Math.round((r.distress || 0) * 100);
  recepBar.style.width = pct + "%";
  recepDistress.textContent = `distress ${pct}%`;
  if (r.distress >= 0.5 && r.session_min >= 5) {
    recepFlag.textContent = "⚠ sustained distress — short break advised";
    recepFlag.className = "recep-flag";
    setPill("elevated", "needs break");
  } else if (r.session_min >= 30) {
    recepFlag.textContent = "long session — hydration check";
    recepFlag.className = "recep-flag";
    setPill("balanced", "long session");
  } else if (r.distress >= 0.35) {
    recepFlag.textContent = "mild distress — monitor";
    recepFlag.className = "recep-flag";
    setPill("balanced", "mild stress");
  } else {
    recepFlag.textContent = "ok";
    recepFlag.className = "recep-flag ok";
    setPill("calm", "steady");
  }
}

// Arms-crossed debounce — state must hold for ARMS_DEBOUNCE_MS before we emit.
const ARMS_DEBOUNCE_MS = 1500;
const armsTrack = { stable: "unknown", pending: "unknown", pendingSince: 0, lastConf: 0 };
let lastPovTs = -1;

async function detectLoop() {
  if (!ready || video.readyState < 2) { requestAnimationFrame(detectLoop); return; }

  let ts = Math.round((video.currentTime || 0) * 1000);
  if (ts <= lastPovTs) ts = lastPovTs + 1;
  lastPovTs = ts;

  let emotionLabel = null;
  let faceBox = null;
  try {
    const faceRes = faceLandmarkerPov.detectForVideo(video, ts);
    const bs = blendshapesFromResult(faceRes);
    if (bs) {
      const m = scoreEmotions(bs);
      emotionLabel = m.label;
    }
    if (faceRes?.faceLandmarks?.length) {
      faceBox = bboxFromLandmarks(faceRes.faceLandmarks[0], video.videoWidth, video.videoHeight);
    }
  } catch {}

  let armsDet = null;
  try {
    const poseRes = poseLandmarker.detectForVideo(video, ts);
    const lm = poseRes?.landmarks?.[0];
    if (lm) armsDet = detectArmsCrossed(lm);
  } catch {}

  frameCount++;
  const now = performance.now();
  if (now - lastFpsAt > 1000) {
    fpsEl.textContent = ((frameCount - lastFpsCount) / ((now - lastFpsAt) / 1000)).toFixed(1) + " fps";
    lastFpsAt = now; lastFpsCount = frameCount;
  }

  if (emotionLabel) {
    lastFaceDetectedAt = Date.now();
    sendEmotion(emotionLabel);
  }

  // Arms-crossed debounce: require the same state ≥1.5s before emitting.
  const armsObserved = armsDet ? (armsDet.crossed ? "crossed" : "open") : "unknown";
  if (armsObserved !== armsTrack.pending) {
    armsTrack.pending = armsObserved;
    armsTrack.pendingSince = now;
    armsTrack.lastConf = armsDet?.confidence || 0;
  } else if (armsObserved !== armsTrack.stable && now - armsTrack.pendingSince > ARMS_DEBOUNCE_MS) {
    armsTrack.stable = armsObserved;
    if (armsObserved === "crossed" || armsObserved === "open") {
      sendBody(armsObserved, armsDet?.confidence);
    }
  }
  updateArmsPill(armsTrack.stable, armsTrack.lastConf);

  // Face recognition descriptor via face-api.js — throttled to ~0.5 Hz. This
  // is the only reason face-api is still loaded; MediaPipe has no identity
  // embedding and ripping out recognition would break /face + /claim.
  if (Date.now() - lastDescriptorAt > 2000) {
    lastDescriptorAt = Date.now();
    try {
      const r = await faceapi
        .detectSingleFace(video, new faceapi.TinyFaceDetectorOptions({ inputSize: 224, scoreThreshold: 0.5 }))
        .withFaceLandmarks()
        .withFaceDescriptor();
      if (r) {
        lastDescriptor = r.descriptor;
        sendDescriptor(r.descriptor);
      }
    } catch {}
  }

  const matchLabel = faceMatchState.patientId
    ? `${(knownName(faceMatchState.patientId) || "patient").toUpperCase()}`
    : null;
  drawBbox(faceBox ? { box: faceBox } : null, matchLabel, emotionLabel);

  setTimeout(detectLoop, 120);   // ~8 Hz — MP runs on GPU, so this is cheap.
}

// --- Queue + visitor + vitals updates from /status ---
let patientsById = {};

function knownName(pid) { return patientsById[pid]?.name; }

function fmtWait(s) {
  if (s == null) return "—";
  const m = Math.floor(s / 60), r = Math.floor(s % 60);
  return m > 0 ? `${m}m ${r}s` : `${r}s`;
}

function waitClass(s) {
  if (s == null) return "";
  const m = s / 60;
  return m < 5 ? "green" : m < 10 ? "amber" : "red";
}

function renderStats(stats, patients) {
  if (!stats) return;
  statWaiting.textContent = stats.count_waiting ?? 0;
  statWaitingSub.textContent = `of ${patients.length} total`;
  statAvg.textContent = stats.avg_wait_s != null ? fmtWait(stats.avg_wait_s) : "—";
  statMaxSub.textContent = stats.max_wait_s != null ? `longest ${fmtWait(stats.max_wait_s)}` : "";
  statUrgent.textContent = stats.count_elevated ?? 0;
  // Toggle urgent emphasis
  document.querySelector(".stat-tile.urgent").style.opacity = (stats.count_elevated > 0) ? 1 : 0.55;
}

const patientOpenState = new Set();   // user-expanded patients persist across renders

function renderPatientList(patients, current, badgeList) {
  patientsById = {};
  for (const p of patients) patientsById[p.id] = p;
  badges = badgeList || [];
  // Sort: at-counter first, then elevated, then by wait desc
  const priorityOf = p => (p.id === current ? 0 : p.anxiety?.label === "elevated" ? 1 : 2);
  const sorted = [...patients].sort((a, b) => priorityOf(a) - priorityOf(b) || (b.wait_s || 0) - (a.wait_s || 0));

  patientListEl.innerHTML = "";
  for (const p of sorted) {
    const anx = p.anxiety?.label || "unknown";
    const det = document.createElement("details");
    det.className = "patient-item " + anx;
    det.dataset.pid = p.id;
    // Keep open if: current visitor, elevated, or user previously opened
    if (p.id === current || anx === "elevated" || patientOpenState.has(p.id)) {
      det.open = true;
    }
    det.addEventListener("toggle", () => {
      if (det.open) patientOpenState.add(p.id);
      else patientOpenState.delete(p.id);
    });

    const waitTxt = p.wait_s == null ? "not checked in" : fmtWait(p.wait_s);
    const waitCls = waitClass(p.wait_s);
    const hrTxt = p.hr ? `${p.hr}` : "—";
    const hrvTxt = p.hrv?.source === "rr"
      ? `RMSSD ${p.hrv.rmssd_ms} ms`
      : p.hrv ? `HR stdev ~${p.hrv.hr_stdev_bpm} bpm (proxy)` : "awaiting samples";
    const anxPill = anx !== "unknown" ? `<span class="pill ${anx}">${anx}</span>` : `<span class="pill unknown">—</span>`;
    const emo = EMOJIS[p.emotion] || "—";
    const assignedBadge = badges.find(b => b.patient_id === p.id);
    const badgePill = assignedBadge ? `<span class="pill badge">${escapeHtml(assignedBadge.label)}</span>` : "";

    det.innerHTML = `
      <summary>
        <span class="pi-name">
          <b>${escapeHtml(p.name)}</b>
          <span class="age">· ${p.age}</span>
          ${p.id === current ? '<span class="at">AT COUNTER</span>' : ''}
        </span>
        <span class="pi-meta">
          <span class="bay">${escapeHtml(p.bay)}</span>
          <span class="hr">${hrTxt}♥</span>
          ${anxPill}
          ${badgePill}
          <span>${emo}</span>
        </span>
        <span class="pi-wait ${waitCls}">${waitTxt}</span>
        <a class="pi-open" href="/patient/${p.id}" target="_blank" onclick="event.stopPropagation()">open record →</a>
      </summary>
      <div class="pi-body">
        <div class="complaint">"${escapeHtml(p.chief_complaint)}"</div>
        <div class="pi-actions">
          <button class="badge-assign-btn ${assignedBadge ? "assigned" : ""}" type="button">
            ${assignedBadge ? `${escapeHtml(assignedBadge.label)} assigned` : "assign badge"}
          </button>
          ${assignedBadge ? '<button class="badge-clear-btn" type="button">clear badge</button>' : ""}
        </div>
        <div class="pi-vitals-row">
          <span class="chip">HR <b>${hrTxt} bpm</b></span>
          <span class="chip">HRV <b>${escapeHtml(hrvTxt)}</b></span>
          <span class="chip">distress <b>${(p.distress ?? 0).toFixed(2)}</b></span>
          <span class="chip">emotion <b>${escapeHtml(p.emotion || "—")}</b></span>
          <span class="chip arms-chip ${p.arms_active ? "crossed" : ""}">arms <b>${escapeHtml(p.arms || "—")}</b></span>
        </div>
        <div class="pi-grid">
          <div>age <b>${p.age}</b></div>
          <div>location <b>${escapeHtml(p.bay)}</b></div>
          <div>allergies <b>${escapeHtml(p.allergies)}</b></div>
          <div>medications <b>${escapeHtml(p.meds)}</b></div>
          <div style="grid-column:1/3;">last visit <b>${escapeHtml(p.last_visit)}</b></div>
        </div>
      </div>`;
    det.querySelector(".badge-assign-btn")?.addEventListener("click", ev => {
      ev.preventDefault();
      ev.stopPropagation();
      assignBadge(p.id);
    });
    det.querySelector(".badge-clear-btn")?.addEventListener("click", ev => {
      ev.preventDefault();
      ev.stopPropagation();
      clearBadge();
    });
    patientListEl.appendChild(det);
  }

  // HR/esp dot reflects whether we have any live HR
  const anyHr = patients.some(p => p.hr);
  dotHr.classList.toggle("on", anyHr);
  dotHr.classList.toggle("off", !anyHr);
}

function initialsOf(name) {
  return name.split(/\s+/).filter(Boolean).map(s => s[0].toUpperCase()).slice(0, 2).join("");
}

function renderFloorPlan(patients, current) {
  const zoneEls = {
    "front desk": document.querySelector(".zone.desk"),
    "bay 1":       document.querySelector(".zone.bay1"),
    "bay 2":       document.querySelector(".zone.bay2"),
  };
  const waitEl = document.querySelector(".zone.wait");
  for (const z of [...Object.values(zoneEls), waitEl]) {
    z.querySelectorAll(".avatar, .zone-empty").forEach(n => n.remove());
  }
  const occ = {};
  nWaiting.textContent = patients.filter(p => p.wait_s != null).length;

  for (const p of patients) {
    const parent = zoneEls[p.bay] || waitEl;
    occ[parent === waitEl ? "wait" : p.bay] = (occ[parent === waitEl ? "wait" : p.bay] || 0) + 1;

    const av = document.createElement("div");
    av.className = "avatar" + (p.id === current ? " at-counter" : "");
    const anx = p.anxiety?.label || "unknown";
    const waitCls = waitClass(p.wait_s);
    const waitTxt = p.wait_s == null ? "new" : fmtWait(p.wait_s);
    const hrTxt = p.hr ?? "—";
    av.innerHTML = `
      <div class="avatar-ring ${anx}" title="${escapeHtml(p.name)} · anxiety ${escapeHtml(anx)}">${initialsOf(p.name)}</div>
      <div class="avatar-name">${escapeHtml(p.name.split(" ")[0])}</div>
      <div class="avatar-hr">${hrTxt} bpm</div>
      <div class="avatar-wait ${waitCls}">${waitTxt}</div>`;
    av.title = `${p.name} — ${p.chief_complaint} — ${p.bay}`;
    parent.appendChild(av);
  }
  for (const [label, el] of Object.entries(zoneEls)) {
    if (!occ[label]) {
      const e = document.createElement("div");
      e.className = "zone-empty"; e.textContent = "empty";
      el.appendChild(e);
    }
  }
  if (!occ["wait"]) {
    const e = document.createElement("div");
    e.className = "zone-empty"; e.textContent = "no patients in waiting area";
    waitEl.appendChild(e);
  }
}

async function pollStatus() {
  try {
    const r = await fetch("/status"); const j = await r.json();
    badges = j.badges || [];
    primaryBadgeId = badges[0]?.id || primaryBadgeId;
    modelName.textContent = j.model;
    dotEsp.classList.toggle("on", j.serial_open);
    dotEsp.classList.toggle("off", !j.serial_open);
    clockEl.textContent = new Date(j.now_ms).toLocaleTimeString();
    renderStats(j.stats, j.patients);
    renderPatientList(j.patients, j.current_visitor, badges);
    update3DPatients(j.patients, j.current_visitor);
    updateClaimPanel(j.patients, j.current_visitor);
    renderReceptionist(j.receptionist);
  } catch (e) { console.warn("pollStatus", e); }
}

// --- Triage ---
function itemKey(it) { return `${it.priority}|${it.patient}|${it.action}`; }

function renderTriageHero(entry) {
  triageItems.innerHTML = "";
  const items = entry.items || [];
  if (items.length === 0) {
    triageItems.innerHTML = `<div class="triage-empty">${escapeHtml(entry.raw || "no items")}</div>`;
    return;
  }
  const newKeys = [];
  for (const it of items) {
    const row = document.createElement("div");
    row.className = "triage-item " + it.priority;
    row.innerHTML = `
      <div class="pri">${it.priority}</div>
      <div class="body"><b>${escapeHtml(it.patient)}</b><br><span class="action">${escapeHtml(it.action)}</span></div>`;
    const k = itemKey(it);
    if (!lastTriageIds.has(k)) row.classList.add("new");
    newKeys.push(k);
    triageItems.appendChild(row);
  }
  // Speak / chime for NEW high-priority items
  for (const it of items) {
    const k = itemKey(it);
    if (it.priority === "HIGH" && !lastTriageIds.has(k)) {
      chime();
      if (voiceChk.checked && "speechSynthesis" in window) {
        const u = new SpeechSynthesisUtterance(`High priority. ${it.patient}. ${it.action}`);
        u.rate = 1.0; u.pitch = 1.0;
        speechSynthesis.speak(u);
      }
    }
  }
  lastTriageIds = new Set(newKeys);
  lastTriageTime.textContent = "updated " + new Date(entry.ts).toLocaleTimeString();
}

async function runTriage() {
  try {
    const r = await fetch("/triage", { method: "POST" });
    const j = await r.json();
    renderTriageHero(j);
  } catch (e) {
    triageItems.innerHTML = `<div class="triage-empty">error: ${escapeHtml(e.message)}</div>`;
  }
}

function triageTick() {
  const remain = Math.max(0, nextTriageAt - Date.now());
  nextTriage.textContent = remain > 0 ? `next in ${Math.ceil(remain / 1000)}s` : "running…";
  if (remain <= 0) {
    scheduleNextTriage();
    runTriage();
  }
}
function scheduleNextTriage() {
  nextTriageAt = Date.now() + (parseInt(triageSec.value, 10) || 30) * 1000;
}

triageNowBtn.addEventListener("click", () => { nextTriageAt = Date.now(); });
triageSec.addEventListener("change", scheduleNextTriage);
resetBtn.addEventListener("click", resetEnrollments);

// Audio chime via WebAudio (more reliable than tiny WAV blob)
let audioCtx = null;
function chime() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(); const g = audioCtx.createGain();
    o.type = "sine"; o.frequency.value = 880;
    g.gain.value = 0.0001;
    g.gain.exponentialRampToValueAtTime(0.25, audioCtx.currentTime + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.35);
    o.connect(g); g.connect(audioCtx.destination);
    o.start(); o.stop(audioCtx.currentTime + 0.4);
  } catch {}
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

// --- 3D waiting room (Three.js) ---
const scene3d = { scene: null, camera: null, renderer: null, patients: {}, ready: false };

const ZONE_POS = {
  "front desk": { x: 0,  z: -3.2, rot: Math.PI },       // facing the camera
  "bay 1":      { x: -5, z: 0,    rot: Math.PI / 2 },   // facing right/center
  "bay 2":      { x: 5,  z: 0,    rot: -Math.PI / 2 },  // facing left/center
};
const WAITING_SLOTS = [
  { x: -3, z: 4.2 }, { x: -1, z: 4.2 }, { x: 1, z: 4.2 }, { x: 3, z: 4.2 },
];
const ANX_COLORS_3D = {
  calm:     0x7ee787,
  balanced: 0xf0883e,
  elevated: 0xff7b72,
  unknown:  0x4c5560,
};
const COUNTER_COLOR = 0xd2a8ff;

function init3D() {
  if (typeof THREE === "undefined") return;
  const container = document.getElementById("waiting-3d");
  if (!container) return;
  const W = container.clientWidth, H = container.clientHeight;

  const scene = new THREE.Scene();
  scene.fog = new THREE.Fog(0x0b0d10, 16, 36);

  const camera = new THREE.PerspectiveCamera(38, W / H, 0.1, 80);
  camera.position.set(9, 8, 11);
  camera.lookAt(0, 0.8, 0);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setSize(W, H);
  renderer.setPixelRatio(devicePixelRatio);
  renderer.setClearColor(0x14181d);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.appendChild(renderer.domElement);

  // Lights
  scene.add(new THREE.AmbientLight(0xffffff, 0.35));
  const key = new THREE.DirectionalLight(0xffffff, 0.85);
  key.position.set(6, 12, 8);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  Object.assign(key.shadow.camera, { left: -15, right: 15, top: 15, bottom: -15 });
  scene.add(key);
  const rim = new THREE.PointLight(COUNTER_COLOR, 0.7, 14);
  rim.position.set(0, 3.5, -4);
  scene.add(rim);
  const warm = new THREE.PointLight(0xf0883e, 0.3, 10);
  warm.position.set(0, 3, 4);
  scene.add(warm);

  // Floor
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(26, 22),
    new THREE.MeshStandardMaterial({ color: 0x1a1f26, roughness: 0.9 })
  );
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);

  const grid = new THREE.GridHelper(26, 13, 0x2a3038, 0x1e232a);
  grid.position.y = 0.01;
  scene.add(grid);

  // Reception desk
  const desk = new THREE.Mesh(
    new THREE.BoxGeometry(7, 1.2, 1.4),
    new THREE.MeshStandardMaterial({ color: 0x2a3038, roughness: 0.4 })
  );
  desk.position.set(0, 0.6, -5);
  desk.castShadow = true; desk.receiveShadow = true;
  scene.add(desk);
  const deskTrim = new THREE.Mesh(
    new THREE.BoxGeometry(7.15, 0.1, 0.25),
    new THREE.MeshStandardMaterial({ color: COUNTER_COLOR, emissive: COUNTER_COLOR, emissiveIntensity: 0.35 })
  );
  deskTrim.position.set(0, 1.2, -5.6);
  scene.add(deskTrim);

  // Bay walls (low dividers)
  for (const [x, z] of [[-6.5, 0], [6.5, 0]]) {
    const wall = new THREE.Mesh(
      new THREE.BoxGeometry(0.15, 1.6, 3),
      new THREE.MeshStandardMaterial({ color: 0x23282f, roughness: 0.7 })
    );
    wall.position.set(x, 0.8, z);
    wall.castShadow = true; wall.receiveShadow = true;
    scene.add(wall);
  }

  // Bay chairs
  const bay1Chair = createChair(); bay1Chair.position.set(-5, 0, 0); bay1Chair.rotation.y = Math.PI / 2; scene.add(bay1Chair);
  const bay2Chair = createChair(); bay2Chair.position.set(5, 0, 0);  bay2Chair.rotation.y = -Math.PI / 2; scene.add(bay2Chair);

  // Waiting-area chairs
  for (const slot of WAITING_SLOTS) {
    const c = createChair();
    c.position.set(slot.x, 0, slot.z);
    c.rotation.y = Math.PI;  // face the desk
    scene.add(c);
  }

  // Floor labels
  scene.add(makeFloorLabel("FRONT DESK", 0, -3.8, 0.9));
  scene.add(makeFloorLabel("BAY 1", -5, 1.2, 0.7));
  scene.add(makeFloorLabel("BAY 2", 5, 1.2, 0.7));
  scene.add(makeFloorLabel("WAITING AREA", 0, 3.2, 0.8));

  Object.assign(scene3d, { scene, camera, renderer, container, ready: true });
  window.addEventListener("resize", onResize3D);
  requestAnimationFrame(animate3D);
}

function onResize3D() {
  if (!scene3d.ready) return;
  const W = scene3d.container.clientWidth, H = scene3d.container.clientHeight;
  scene3d.camera.aspect = W / H;
  scene3d.camera.updateProjectionMatrix();
  scene3d.renderer.setSize(W, H);
}

function createChair() {
  const g = new THREE.Group();
  const seatMat = new THREE.MeshStandardMaterial({ color: 0x3a414a, roughness: 0.6 });
  const legMat  = new THREE.MeshStandardMaterial({ color: 0x22262c, roughness: 0.8 });
  const seat = new THREE.Mesh(new THREE.BoxGeometry(0.7, 0.1, 0.7), seatMat);
  seat.position.y = 0.45; seat.castShadow = true; seat.receiveShadow = true;
  g.add(seat);
  const back = new THREE.Mesh(new THREE.BoxGeometry(0.7, 0.7, 0.1), seatMat);
  back.position.set(0, 0.8, -0.3); back.castShadow = true;
  g.add(back);
  const legGeo = new THREE.BoxGeometry(0.06, 0.45, 0.06);
  for (const [x, z] of [[-0.27, -0.27], [0.27, -0.27], [-0.27, 0.27], [0.27, 0.27]]) {
    const leg = new THREE.Mesh(legGeo, legMat);
    leg.position.set(x, 0.22, z); leg.castShadow = true;
    g.add(leg);
  }
  return g;
}

function makeFloorLabel(text, x, z, size = 0.8) {
  const canvas = document.createElement("canvas");
  canvas.width = 512; canvas.height = 80;
  const ctx = canvas.getContext("2d");
  ctx.font = "bold 36px ui-monospace, monospace";
  ctx.fillStyle = "rgba(122,134,145,0.6)";
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(text, 256, 40);
  const tex = new THREE.CanvasTexture(canvas);
  const mat = new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false });
  const geo = new THREE.PlaneGeometry(size * 3, size * (80 / 512) * 3);
  const mesh = new THREE.Mesh(geo, mat);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.set(x, 0.02, z);
  return mesh;
}

function createPatientMesh(name) {
  const group = new THREE.Group();

  // Body (seated: shorter capsule)
  const body = new THREE.Mesh(
    new THREE.CapsuleGeometry(0.28, 0.55, 4, 10),
    new THREE.MeshStandardMaterial({ color: 0x4a5058, roughness: 0.6 })
  );
  body.position.y = 0.95; body.castShadow = true;
  group.add(body);

  // Head
  const head = new THREE.Mesh(
    new THREE.SphereGeometry(0.22, 16, 14),
    new THREE.MeshStandardMaterial({ color: 0xd8b797, roughness: 0.5 })
  );
  head.position.y = 1.45; head.castShadow = true;
  group.add(head);

  // Anxiety/state ring on floor
  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(0.6, 0.05, 8, 40),
    new THREE.MeshStandardMaterial({
      color: 0x7ee787, emissive: 0x7ee787, emissiveIntensity: 0.5,
      transparent: true, opacity: 0.85,
    })
  );
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.06;
  group.add(ring);

  // Floating label
  const label = makeSpriteLabel(name + " · — bpm");
  label.position.set(0, 2.1, 0);
  group.add(label);

  return { group, body, head, ring, label };
}

function makeSpriteLabel(text) {
  const canvas = document.createElement("canvas");
  canvas.width = 512; canvas.height = 140;
  drawLabel(canvas, text, "#e6edf3", "#222931");
  const tex = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false }));
  sprite.scale.set(2.4, 0.65, 1);
  sprite.renderOrder = 999;
  sprite.userData = { canvas };
  return sprite;
}

function drawLabel(canvas, text, textColor, borderColor) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "rgba(11,13,16,0.92)";
  ctx.fillRect(4, 4, canvas.width - 8, canvas.height - 8);
  ctx.strokeStyle = borderColor; ctx.lineWidth = 4;
  ctx.strokeRect(4, 4, canvas.width - 8, canvas.height - 8);
  ctx.font = "bold 38px ui-monospace, monospace";
  ctx.fillStyle = textColor;
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(text, canvas.width / 2, canvas.height / 2);
}

function updateSpriteLabel(sprite, text, borderColor) {
  drawLabel(sprite.userData.canvas, text, "#e6edf3", borderColor);
  sprite.material.map.needsUpdate = true;
}

function zoneFor(patient, waitingIdx) {
  const pos = ZONE_POS[patient.bay];
  if (pos) return { x: pos.x, z: pos.z, rot: pos.rot };
  const slot = WAITING_SLOTS[waitingIdx % WAITING_SLOTS.length];
  return { x: slot.x, z: slot.z, rot: Math.PI };
}

function update3DPatients(patients, current) {
  if (!scene3d.ready) return;

  const present = new Set(patients.map(p => p.id));
  for (const pid of Object.keys(scene3d.patients)) {
    if (!present.has(pid)) {
      scene3d.scene.remove(scene3d.patients[pid].group);
      delete scene3d.patients[pid];
    }
  }

  let waitingIdx = 0;
  for (const p of patients) {
    let m = scene3d.patients[p.id];
    if (!m) {
      m = createPatientMesh(p.name);
      scene3d.patients[p.id] = m;
      scene3d.scene.add(m.group);
    }
    const useWaiting = !ZONE_POS[p.bay];
    const z = zoneFor(p, waitingIdx);
    if (useWaiting) waitingIdx++;
    m.group.position.set(z.x, 0, z.z);
    m.group.rotation.y = z.rot;

    const anx = p.anxiety?.label || "unknown";
    const isCurrent = p.id === current;
    const color = isCurrent ? COUNTER_COLOR : (ANX_COLORS_3D[anx] || ANX_COLORS_3D.unknown);
    m.ring.material.color.setHex(color);
    m.ring.material.emissive.setHex(color);
    m.body.material.color.setHex(isCurrent ? 0x2a2d3a : 0x4a5058);

    const hrTxt = p.hr ? `${p.hr} bpm` : "—";
    const nameTxt = `${p.name.split(" ")[0]}  ${hrTxt}`;
    updateSpriteLabel(m.label, nameTxt, "#" + color.toString(16).padStart(6, "0"));

    m.group.userData = { anxiety: anx, isCurrent, hr: p.hr || 60 };
  }
}

function animate3D(t) {
  if (!scene3d.ready) return;
  // Subtle camera sway so it feels alive
  const tc = t / 1000;
  scene3d.camera.position.x = 9 + Math.sin(tc / 6) * 0.8;
  scene3d.camera.position.z = 11 + Math.cos(tc / 7) * 0.4;
  scene3d.camera.position.y = 8 + Math.sin(tc / 5) * 0.15;
  scene3d.camera.lookAt(0, 0.8, 0);

  for (const m of Object.values(scene3d.patients)) {
    const data = m.group.userData;
    // Breathing scale on body
    const breath = Math.sin(tc * 1.2) * 0.03;
    m.body.scale.set(1, 1 + breath, 1);
    // Heart-beat pulse on ring — synced to BPM
    const bps = Math.max(40, data.hr || 60) / 60;
    const beat = Math.max(0, Math.sin(tc * Math.PI * 2 * bps));
    const base = data.anxiety === "elevated" ? 0.6 : 0.4;
    m.ring.material.emissiveIntensity = base + beat * 0.6;
    m.ring.scale.set(1 + beat * 0.05, 1 + beat * 0.05, 1);
    // At-counter: gentle float + stronger glow
    if (data.isCurrent) {
      m.group.position.y = Math.abs(Math.sin(tc * 1.4)) * 0.08;
      m.ring.material.emissiveIntensity = 0.7 + beat * 0.4;
    } else {
      m.group.position.y = 0;
    }
  }

  scene3d.renderer.render(scene3d.scene, scene3d.camera);
  requestAnimationFrame(animate3D);
}

// --- Init ---
(async () => {
  init3D();
  await pollStatus();
  await startCamera();
  resizeOverlay();
  await loadModels();
  scheduleNextTriage();
  detectLoop();
  detectLoopReceptionist();
  setInterval(pollStatus, 1000);
  setInterval(triageTick, 250);
  navigator.mediaDevices.addEventListener?.("devicechange", listCameras);
})();
</script>
</body>
</html>
"""


PATIENT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>patient record · HappyClinic</title>
<style>
  :root {
    --bg:#0b0d10; --panel:#14181d; --panel2:#191e25; --line:#222931;
    --text:#e6edf3; --mute:#7a8691; --faint:#4c5560;
    --ok:#7ee787; --warn:#f0883e; --err:#ff7b72; --claude:#d2a8ff; --hr:#ff5d73;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body {
    background: var(--bg); color: var(--text);
    font: 13px/1.45 ui-monospace, SF Mono, Menlo, Consolas, monospace;
    min-height: 100vh;
  }
  header {
    padding: 14px 24px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; gap: 18px;
    background: var(--panel);
  }
  header a { color: var(--mute); text-decoration: none; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; }
  header a:hover { color: var(--claude); }
  header h1 { margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -0.01em; }
  header .sub { color: var(--mute); font-size: 12px; margin-left: 6px; }
  header .pill { margin-left: auto; font-size: 11px; padding: 4px 12px; border-radius: 12px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; }
  .pill.calm { background: var(--ok); color: #0b0d10; }
  .pill.balanced { background: var(--warn); color: #0b0d10; }
  .pill.elevated { background: var(--err); color: #fff; }
  .pill.unknown { border: 1px solid var(--line); color: var(--mute); }

  main { max-width: 1100px; margin: 0 auto; padding: 22px 24px 40px; display: grid; gap: 14px; }

  .complaint-bar {
    background: var(--panel); border: 1px solid var(--line); padding: 14px 18px;
    font-family: ui-serif, Georgia, Cambria, serif;
    font-size: 18px; color: var(--text); font-style: italic;
    border-left: 3px solid var(--claude);
  }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }

  .card { background: var(--panel); border: 1px solid var(--line); padding: 16px 18px; }
  .card h2 {
    margin: 0 0 14px; font-size: 10px; font-weight: 700;
    letter-spacing: 0.12em; text-transform: uppercase; color: var(--mute);
    display: flex; justify-content: space-between; align-items: baseline;
  }

  .vital-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .vital {
    background: var(--panel2); border: 1px solid var(--line); padding: 10px 12px;
    display: flex; flex-direction: column; gap: 3px;
  }
  .vital .label { font-size: 9px; color: var(--mute); letter-spacing: 0.12em; text-transform: uppercase; }
  .vital .big { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.1; }
  .vital .sub { font-size: 10px; color: var(--mute); }
  .vital.hr .big { color: var(--hr); }
  .vital.hr .heart { display:inline-block; color: var(--hr); font-size: 14px; margin-right: 3px; animation: beat 1s infinite ease-in-out; }
  @keyframes beat { 0%,100%{transform:scale(1);} 20%{transform:scale(1.18);} 40%{transform:scale(1);} 60%{transform:scale(1.08);} }

  .ehr-row { display: grid; grid-template-columns: 110px 1fr; gap: 10px; padding: 5px 0; font-size: 12px; color: var(--mute); border-bottom: 1px dashed var(--line); }
  .ehr-row:last-child { border-bottom: none; }
  .ehr-row b { color: var(--text); font-weight: 500; }

  canvas.chart { display: block; width: 100%; height: 140px; }

  .emo-segments { display: flex; gap: 1px; height: 26px; background: var(--panel2); border: 1px solid var(--line); }
  .emo-segments span { flex: 1; }

  .triage-list { display: flex; flex-direction: column; gap: 6px; max-height: 280px; overflow-y: auto; }
  .triage-row {
    display: grid; grid-template-columns: 66px 70px 1fr; gap: 10px; align-items: baseline;
    padding: 7px 10px; border-left: 3px solid var(--faint);
    background: rgba(255,255,255,0.02); font-size: 12px;
  }
  .triage-row.HIGH { border-left-color: var(--err); background: rgba(255,123,114,0.07); }
  .triage-row.MED { border-left-color: var(--warn); }
  .triage-row.LOW { border-left-color: var(--ok); }
  .triage-row .t { color: var(--mute); font-size: 10px; }
  .triage-row .pri { font-size: 10px; font-weight: 700; letter-spacing: 0.1em; }
  .triage-row.HIGH .pri { color: var(--err); }
  .triage-row.MED  .pri { color: var(--warn); }
  .triage-row.LOW  .pri { color: var(--ok); }
  .triage-empty { color: var(--mute); font-size: 12px; font-style: italic; padding: 10px; text-align: center; }

  footer { color: var(--faint); font-size: 10px; text-align: center; padding: 10px; letter-spacing: 0.1em; text-transform: uppercase; }
</style>
</head>
<body>
<header>
  <a href="/">← reception</a>
  <h1 id="p-name">—</h1>
  <span class="sub" id="p-sub">—</span>
  <span class="pill unknown" id="p-pill">—</span>
</header>

<main>
  <div class="complaint-bar" id="complaint">loading chief complaint…</div>

  <div class="grid-2">
    <div class="card">
      <h2>vital signs <span id="hr-fresh" style="color:var(--ok);font-weight:400;font-size:10px;text-transform:none;letter-spacing:0;">— live</span></h2>
      <div class="vital-grid">
        <div class="vital hr">
          <div class="label"><span class="heart">♥</span>heart rate</div>
          <div class="big" id="v-hr">—</div>
          <div class="sub" id="v-hr-sub">awaiting signal</div>
        </div>
        <div class="vital">
          <div class="label">anxiety · HRV-derived</div>
          <div><span class="pill unknown" id="v-anx" style="font-size:13px;padding:4px 10px;">—</span></div>
          <div class="sub" id="v-anx-sub">—</div>
        </div>
        <div class="vital">
          <div class="label">distress index</div>
          <div class="big" id="v-distress">—</div>
          <div class="sub">0 calm · 1 distressed</div>
        </div>
        <div class="vital">
          <div class="label">current emotion</div>
          <div class="big" style="font-size:30px;" id="v-emoji">—</div>
          <div class="sub" id="v-emotion">—</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>clinical record</h2>
      <div class="ehr-row"><span>age</span><b id="ehr-age">—</b></div>
      <div class="ehr-row"><span>location</span><b id="ehr-bay">—</b></div>
      <div class="ehr-row"><span>wait time</span><b id="ehr-wait">—</b></div>
      <div class="ehr-row"><span>checked in</span><b id="ehr-checked">—</b></div>
      <div class="ehr-row"><span>allergies</span><b id="ehr-allergies">—</b></div>
      <div class="ehr-row"><span>medications</span><b id="ehr-meds">—</b></div>
      <div class="ehr-row"><span>last visit</span><b id="ehr-last">—</b></div>
    </div>
  </div>

  <div class="card">
    <h2>heart rate · last 3 min <span id="hr-range" style="color:var(--text);text-transform:none;letter-spacing:0;">—</span></h2>
    <canvas class="chart" id="hr-chart" width="800" height="140"></canvas>
  </div>

  <div class="card">
    <h2>emotion timeline · last 3 min</h2>
    <div class="emo-segments" id="emo-seg"></div>
    <div style="margin-top:8px;display:flex;gap:14px;font-size:10px;color:var(--mute);letter-spacing:0.05em;">
      <span><i style="background:#7ee787;display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px;"></i>happy</span>
      <span><i style="background:#79c0ff;display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px;"></i>sad</span>
      <span><i style="background:#ff7b72;display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px;"></i>angry</span>
      <span><i style="background:#f0883e;display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px;"></i>surprised</span>
      <span><i style="background:#6e7781;display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px;"></i>neutral</span>
    </div>
  </div>

  <div class="card">
    <h2>claude triage actions · mentioning this patient</h2>
    <div class="triage-list" id="triage-list">
      <div class="triage-empty">no actions yet</div>
    </div>
  </div>
</main>

<footer>HappyClinic reception · auto-refresh 1s</footer>

<script>
const PID = window.location.pathname.split("/").filter(Boolean).pop();
const EMO_COLORS = { happy:"#7ee787", sad:"#79c0ff", angry:"#ff7b72", surprised:"#f0883e", neutral:"#6e7781" };
const EMOJIS = { happy:"😊", sad:"😢", angry:"😠", surprised:"😮", neutral:"😐" };

const el = id => document.getElementById(id);

function fmtWait(s) {
  if (s == null) return "—";
  const m = Math.floor(s / 60), r = Math.floor(s % 60);
  return m > 0 ? `${m}m ${r}s` : `${r}s`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function drawHrChart(series) {
  const canvas = el("hr-chart");
  const r = canvas.getBoundingClientRect();
  canvas.width = r.width * devicePixelRatio;
  canvas.height = r.height * devicePixelRatio;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!series || series.length < 2) {
    ctx.fillStyle = "#7a8691"; ctx.font = `${11*devicePixelRatio}px monospace`;
    ctx.fillText("collecting data…", 10 * devicePixelRatio, 20 * devicePixelRatio);
    el("hr-range").textContent = "—";
    return;
  }
  const xs = series.map(d => d[0]); const ys = series.map(d => d[1]);
  const t0 = xs[0], t1 = xs[xs.length-1];
  const ymin = Math.min(...ys) - 3, ymax = Math.max(...ys) + 3;
  const pad = 14 * devicePixelRatio;
  const px = t => pad + ((t-t0)/Math.max(1,t1-t0)) * (canvas.width - 2*pad);
  const py = v => canvas.height - pad - ((v-ymin)/Math.max(1,ymax-ymin)) * (canvas.height - 2*pad);

  // Grid lines for HR zones
  ctx.strokeStyle = "#222931"; ctx.lineWidth = 1;
  for (const v of [60, 80, 100, 120]) {
    if (v < ymin || v > ymax) continue;
    ctx.beginPath(); ctx.moveTo(pad, py(v)); ctx.lineTo(canvas.width - pad, py(v)); ctx.stroke();
    ctx.fillStyle = "#4c5560"; ctx.font = `${9*devicePixelRatio}px monospace`;
    ctx.fillText(v.toString(), canvas.width - pad + 2, py(v) + 3*devicePixelRatio);
  }

  // Area
  ctx.beginPath();
  ctx.moveTo(px(xs[0]), canvas.height - pad);
  for (let i = 0; i < xs.length; i++) ctx.lineTo(px(xs[i]), py(ys[i]));
  ctx.lineTo(px(xs[xs.length-1]), canvas.height - pad);
  ctx.closePath();
  ctx.fillStyle = "rgba(255,93,115,0.15)";
  ctx.fill();

  // Line
  ctx.strokeStyle = "#ff5d73"; ctx.lineWidth = 2.4 * devicePixelRatio;
  ctx.beginPath();
  for (let i = 0; i < xs.length; i++) { i ? ctx.lineTo(px(xs[i]), py(ys[i])) : ctx.moveTo(px(xs[0]), py(ys[0])); }
  ctx.stroke();

  // Latest dot
  ctx.fillStyle = "#ff5d73";
  ctx.beginPath(); ctx.arc(px(xs[xs.length-1]), py(ys[ys.length-1]), 4*devicePixelRatio, 0, Math.PI*2); ctx.fill();

  const avg = ys.reduce((a,b)=>a+b,0)/ys.length;
  el("hr-range").textContent = `${Math.min(...ys)}–${Math.max(...ys)} bpm · avg ${avg.toFixed(1)}`;
}

function renderEmoStrip(history) {
  const container = el("emo-seg");
  container.innerHTML = "";
  const now = Date.now();
  const start = now - 3 * 60 * 1000;  // last 3 min
  const recent = (history || []).filter(e => e.ts >= start);
  if (recent.length === 0) {
    container.innerHTML = `<span style="flex:1;background:#191e25;display:flex;align-items:center;justify-content:center;color:#4c5560;font-size:11px;">no samples in last 3 min</span>`;
    return;
  }
  for (let i = 0; i < recent.length; i++) {
    const segStart = recent[i].ts;
    const segEnd = i + 1 < recent.length ? recent[i+1].ts : now;
    const widthPct = ((segEnd - segStart) / (now - start)) * 100;
    const s = document.createElement("span");
    s.style.flex = "none";
    s.style.width = widthPct.toFixed(2) + "%";
    s.style.background = EMO_COLORS[recent[i].emotion] || "#6e7781";
    s.title = `${recent[i].emotion} @ ${new Date(segStart).toLocaleTimeString()}`;
    container.appendChild(s);
  }
}

async function poll() {
  try {
    const r = await fetch("/status");
    const j = await r.json();
    const p = j.patients.find(x => x.id === PID);
    if (!p) { el("p-name").textContent = "unknown patient"; return; }

    // Header
    el("p-name").textContent = p.name;
    el("p-sub").textContent = `· age ${p.age} · ${p.bay}`;
    const anx = p.anxiety?.label || "unknown";
    const pill = el("p-pill");
    pill.className = "pill " + anx;
    pill.textContent = anx === "unknown" ? "—" : anx;

    // Complaint
    el("complaint").textContent = `"${p.chief_complaint}"`;

    // Vitals
    el("v-hr").textContent = p.hr ?? "—";
    el("v-hr-sub").textContent = p.hr ? "live · garmin" : "no signal";
    document.querySelector(".vital.hr .heart").style.animationDuration = p.hr ? (60/p.hr).toFixed(2) + "s" : "1s";

    const anxEl = el("v-anx");
    anxEl.className = "pill " + anx;
    anxEl.style.fontSize = "13px"; anxEl.style.padding = "4px 10px";
    anxEl.textContent = anx === "unknown" ? "—" : anx;
    el("v-anx-sub").textContent = p.anxiety?.metric ? (p.anxiety.metric + (p.anxiety.proxy ? " · proxy" : "")) : "awaiting samples";

    el("v-distress").textContent = (p.distress ?? 0).toFixed(2);
    el("v-emoji").textContent = EMOJIS[p.emotion] || "—";
    el("v-emotion").textContent = p.emotion || "no face";

    // EHR
    el("ehr-age").textContent = p.age;
    el("ehr-bay").textContent = p.bay;
    el("ehr-wait").textContent = p.wait_s == null ? "not checked in" : fmtWait(p.wait_s);
    el("ehr-checked").textContent = p.checked_in
      ? new Date(p.checked_in * 1000).toLocaleTimeString()
      : "—";
    el("ehr-allergies").textContent = p.allergies;
    el("ehr-meds").textContent = p.meds;
    el("ehr-last").textContent = p.last_visit;

    // HR chart
    drawHrChart(p.hr_series);

    // Emotion timeline — we don't get history per patient from status, approximate from hr_series timestamps (not perfect)
    // Backend doesn't currently expose per-patient emotion history; synthesize from current emotion if none
    // Use what we have: the dashboard's status endpoint doesn't include per-patient emotion_history.
    // Fallback: single current emotion spanning all. Show empty if nothing.
    // For a nicer view, we'd need /patient/<id>/history — skipping for hackathon simplicity.
    if (p.emotion) {
      el("emo-seg").innerHTML = `<span style="flex:1;background:${EMO_COLORS[p.emotion] || "#6e7781"};"></span>`;
    }

    // Triage actions filtered to this patient
    const list = el("triage-list");
    list.innerHTML = "";
    const nameLower = p.name.toLowerCase();
    const mentions = [];
    for (const entry of (j.triage || [])) {
      for (const it of (entry.items || [])) {
        if (it.patient && it.patient.toLowerCase().includes(p.name.split(" ")[0].toLowerCase())) {
          mentions.push({ ts: entry.ts, ...it });
        }
      }
    }
    if (mentions.length === 0) {
      list.innerHTML = `<div class="triage-empty">no triage actions mentioning ${escapeHtml(p.name)} yet — trigger one from the dashboard</div>`;
    } else {
      for (const m of mentions.reverse()) {
        const row = document.createElement("div");
        row.className = "triage-row " + m.priority;
        const t = new Date(m.ts).toLocaleTimeString();
        row.innerHTML = `
          <div class="t">${t}</div>
          <div class="pri">${m.priority}</div>
          <div>${escapeHtml(m.action)}</div>`;
        list.appendChild(row);
      }
    }
  } catch (e) { console.warn("poll", e); }
}

poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    init_face_db()
    open_serial()
    threading.Thread(target=serial_reader, daemon=True).start()
    for pid, p in PATIENTS.items():
        if p["vitals_source"].startswith("sim_"):
            threading.Thread(
                target=simulate_patient, args=(pid, p["vitals_source"]), daemon=True
            ).start()
    print(
        f"\n  HappyClinic reception ready: http://{LISTEN_HOST}:{LISTEN_PORT}"
        "  (bind host configurable via HAPPYCLINIC_LISTEN_HOST)"
    )
    print(f"  patients seeded: {', '.join(p['name'] for p in PATIENTS.values())}")
    print(f"  triage model: {MODEL}, serial port: {PORT}, serial: {'ok' if ser else 'offline'}\n")
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False)
