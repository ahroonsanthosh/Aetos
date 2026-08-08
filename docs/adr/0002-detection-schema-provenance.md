# ADR 0002 — Detection provenance, and the guard test that keeps sim and real interchangeable

- **Status:** Accepted
- **Date:** 2026-08-08

## Context

The single most important architectural rule in Aetos is that **the tracking engine must
not be able to tell whether a detection came from the simulator or from a physical sensor.**

The canonical Detection schema carries a `kinematics` block (`speed_mps`, `heading_deg`)
and a `classification` block (`label`, `confidence`). This creates a specific and serious
hazard.

Most real sensors do not measure those quantities:

- A radar measures range, azimuth, elevation and — sometimes — *radial* Doppler velocity.
  It does not measure heading. Speed and heading are quantities a **tracker derives** from
  a sequence of positions.
- A camera measures a bearing and, with a classifier attached, may genuinely report a
  label. An acoustic sensor may report a bearing and little else.

The simulator, by contrast, knows the ground truth exactly. If nothing prevents it, the
simulator will populate `kinematics` with perfect velocity and `classification` with the
true label. The tracker will start consuming that data because it is present and accurate.
Every test will pass. The day a real radar is connected, tracking quality collapses, and no
test in the suite will have caught it — because the failure is that we were quietly
training the system on information real sensors never supply.

The same hazard applies to classification: if the simulator writes ground-truth labels into
detections and the classification module reads them, that module is a passthrough and its
entire test suite is measuring nothing.

## Decision

1. `kinematics` and `classification` remain in the canonical Detection schema as specified,
   but both are **explicitly optional**.
2. Every optional inferred field carries a **provenance** marker:
   - `measured` — the sensor directly measured this quantity.
   - `derived` — the adapter computed it from that sensor's own measurements.
   - `simulated_truth` — it came from simulator ground truth. Never authoritative.
3. **The V0 tracker does not consume `kinematics` for state estimation.** Velocity is
   estimated by the Kalman filter from the position sequence alone.
4. **Detection-level `classification` is evidence, never a verdict.** The classification
   module treats it as one weighted input among several, discounted by provenance, and is
   required to produce a defensible label without it.
5. **A guard test enforces 1–4.** `tests/integration/test_detection_provenance_guard.py`
   runs a full scenario twice — once with detections as produced, once with `kinematics`
   and `classification` stripped from every detection — and asserts the resulting track
   states are identical.

## Rationale

Point 5 is the part that actually works. Points 1–4 are conventions, and conventions decay
under deadline pressure. A failing test does not decay. If someone later wires the tracker
to `detection.kinematics.speed_mps` to make a scenario pass, the guard test goes red and
names the reason.

## Consequences

- The simulator must be honest: it degrades ground truth through the configured sensor
  model (measurement noise, probability of detection, classification confusion) before
  emitting a Detection. Emitting clean truth is a bug, not a shortcut.
- Tracking startup is slower than it would be with velocity handed to us — the filter needs
  several detections to converge on a velocity estimate. This is the correct behaviour and
  matches what real sensors will produce.
- When a real sensor genuinely does measure a quantity (radar Doppler), a future ADR may
  admit it into the filter as a proper measurement with its own uncertainty — as a
  *measurement*, gated on provenance `measured`, never as a free answer.

## Alternatives considered

- **Implement the schema literally, let the tracker use whatever is present.** Faster to a
  working demo. Rejected: it defeats the project's primary architectural rule, and the
  damage is invisible until real hardware arrives.
- **Remove kinematics and classification from Detection entirely.** Cleanest separation of
  measurement from inference. Rejected: it discards data real sensors genuinely produce
  (EO/IR classification, radar Doppler) and deviates from the written specification.
