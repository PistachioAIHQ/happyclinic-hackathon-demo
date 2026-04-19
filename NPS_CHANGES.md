# NPS Satisfaction Prediction — Integration Summary

## What was added

A lightweight, hardcoded Net Promoter Score (NPS) prediction system integrated into the HappyClinic dashboard. Inspired by the Bayesian causal model in `patient-timeline-sim-v2-streamlit`, but simplified to a weighted formula with zero new dependencies.

NPS ranges from -100 (all detractors) to +100 (all promoters). In healthcare, NPS > 50 is excellent, 0-50 is good, < 0 is poor.

## How NPS is computed

`compute_nps(snap)` in `tools/dashboard.py` uses a weighted penalty formula:

| Factor | Effect |
|--------|--------|
| Baseline | Start at +60 |
| Wait time | -3 per minute, extra -2/min after 10 min |
| Anxiety: calm | 0 |
| Anxiety: balanced | -15 |
| Anxiety: elevated | -30 |
| Distress (0-1) | -(distress x 25) |
| Emotion: happy | +10 |
| Emotion: neutral | 0 |
| Emotion: sad | -10 |
| Emotion: surprised | -5 |
| Emotion: angry | -20 |

Result is clamped to [-100, +100]. Wrapped in try/except so any failure returns `None` (dashboard shows "—").

## Hardcoded interventions

`top_intervention(snap)` returns the best applicable intervention from a fixed table:

| Intervention | NPS Boost | Condition |
|---|---|---|
| Add triage nurse | +18 | Wait > 10 min |
| Comm coaching | +12 | Always |
| Standardized discharge | +8 | Always |
| Comfort rounding | +6 | Distress > 0.4 |

These are inspired by the causal model's do-operator interventions but use fixed deltas instead of Bayesian inference.

## Where NPS appears in the dashboard

### 1. Stats tile (4th tile, top of right column)
- Shows average NPS across all patients in purple
- Sub-label shows zone: "promoter zone" (>=50), "passive zone" (0-49), "detractor zone" (<0)

### 2. Patient accordion cards
- **Summary row**: colored pill next to the anxiety pill — green (NPS >= 50), yellow (0-49), red (< 0)
- **Expanded vitals row**: NPS chip alongside HR, HRV, distress, emotion
- **NPS timeline chart**: live sparkline in each expanded card showing NPS trend over the last 3 minutes. Line color matches current NPS zone (green/yellow/red). Gradient fill from green (top) to red (bottom) with a zero-line reference. Updates every poll cycle (~1 Hz). Endpoint dot + value label show current reading.

### 3. Claude triage prompt
Each patient line now ends with NPS and top intervention:
```
- Mark Chen, 52: "Sharp chest pain..." | bay 2, waiting 9.5 min | HR 96 bpm | anxiety elevated (0.78) | distress 0.65 | emotion neutral | NPS -18 (top: Add triage nurse → +18)
```
Claude naturally references satisfaction in its triage bullets.

### 4. /status JSON endpoint
`stats.avg_nps` field added to the response. Each patient snapshot includes `nps` (integer) and `nps_series` (array of `[ts_ms, nps]` pairs, last 3 min at ~1 Hz).

## Files changed

Only `tools/dashboard.py` — all Python, HTML, CSS, and JS changes are inline in this single file.

## Expected demo behavior

| Patient | Typical NPS | Why |
|---|---|---|
| Priya Singh (calm, short wait) | +55 to +65 | Green pill, promoter |
| Ana Ortiz (moderate) | +20 to +40 | Yellow pill, passive |
| Mark Chen (elevated anxiety, long wait) | -15 to -25 | Red pill, detractor |

NPS drifts downward in real-time as wait times grow, creating visible urgency during the demo.
