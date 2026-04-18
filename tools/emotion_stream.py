#!/usr/bin/env python3
"""Capture webcam → Claude vision → emotion → ESP32 OLED over serial."""
import base64
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import serial
from anthropic import Anthropic

PORT = "/dev/cu.usbserial-0001"
BAUD = 115200
CAM = "0"  # avfoundation device index for FaceTime HD Camera
MODEL = "claude-haiku-4-5-20251001"
INTERVAL = 2.5  # seconds between classifications
EMOTIONS = ["happy", "sad", "angry", "surprised", "neutral", "love"]

SYSTEM = (
    "You classify the facial emotion of the person in the image. "
    f"Reply with EXACTLY ONE word from this list: {', '.join(EMOTIONS)}. "
    "Lowercase. No punctuation. No explanation. "
    "If no person is visible, reply: neutral."
)

client = Anthropic()


def capture(path: Path) -> None:
    # Capture 15 frames and keep only the last — lets the camera warm up.
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "avfoundation",
            "-framerate", "30", "-video_size", "640x480",
            "-i", CAM,
            "-frames:v", "15", "-update", "1",
            str(path),
        ],
        check=True,
    )


def classify(path: Path) -> str:
    b64 = base64.standard_b64encode(path.read_bytes()).decode()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    },
                },
                {"type": "text", "text": "emotion?"},
            ],
        }],
    )
    word = resp.content[0].text.strip().lower().split()[0].strip(".,!?")
    return word if word in EMOTIONS else "neutral"


def main() -> None:
    ser = serial.Serial(PORT, BAUD, timeout=1)
    time.sleep(2)  # ESP32 resets on port open; wait for boot
    print(f"serial open on {PORT}; classifying every {INTERVAL}s (Ctrl-C to stop)")

    frame = Path(tempfile.gettempdir()) / "emotion_frame.jpg"
    try:
        while True:
            t0 = time.time()
            try:
                capture(frame)
                emo = classify(frame)
            except subprocess.CalledProcessError as e:
                print(f"capture failed: {e}", file=sys.stderr)
                time.sleep(1)
                continue
            except Exception as e:
                print(f"classify failed: {e}", file=sys.stderr)
                emo = "neutral"

            dt = time.time() - t0
            print(f"[{dt:4.1f}s] -> {emo}")
            ser.write(f"emotion:{emo}\n".encode())
            ser.flush()

            sleep_for = max(0.0, INTERVAL - dt)
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
