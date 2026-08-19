# `backend/tracking` — where things are

## Responsibility

Turn a stream of detections into a set of tracks: which returns belong to the same object, where
that object is now, how fast it is going, and whether we still believe in it.

This module answers **where**. It does not answer *what* (that is `backend/classification`) or
*how dangerous* (`backend/risk`). Keeping those three questions in three modules is what lets any
of them be replaced without touching the others.

## Interface boundary

**Provides:** `TrackManager` (the cycle), `Track` and its sub-models (the published shape),
`TrackState` (the lifecycle vocabulary).

**Depends on:** `backend.common` for the clock and the local frame, `backend.events` to record
what it did, and nothing else. It does not import risk, classification, zones or the API.

**Consumes:** `IncomingDetection` — a position, a covariance, a timestamp and a sensor id, in the
site's local ENU frame. Note what is *absent*: no speed, no heading, no classification label.
Ingestion performs that reduction, so the tracker is structurally incapable of consuming the
fields ADR 0002 forbids it from using. This is the enforcement, not the convention.

## The cycle

One call to `TrackManager.process(detections)` does, in order:

1. **Predict** every existing track forward to now, so association compares like with like.
   Associating against stale positions makes a fast object look like a mismatch and splits it.
2. **Associate** — solve the whole assignment problem at once, over Mahalanobis distances.
3. **Update** the tracks that got a detection; **coast** those that did not; **initiate** new
   tracks from detections nothing claimed.
4. **Retire** whatever elapsed time has earned.

Every step emits events. The event log carries a full track snapshot on each one, which is what
makes replay reproduce state rather than approximate it (ADR 0006).

## Sub-modules

| Path | Responsibility |
|---|---|
| `filters/kalman.py` | Constant-velocity Kalman filter, ENU, position-only measurement model |
| `association/gnn.py` | Global nearest neighbour with chi-squared gating (ADR 0005) |
| `prediction/` | Forward projection of a track for display and zone-crossing estimates |
| `track_manager/manager.py` | The cycle above, the lifecycle state machine, and event emission |
| `models.py` | `Track` and friends — the published, immutable shape |

## The decisions worth knowing before you change anything here

**The measurement model is position only.** The observation matrix has structurally zero velocity
columns. A detection's `kinematics` block cannot reach the estimate even if a caller passes one,
because there is no parameter through which to pass it. Velocity is inferred from a sequence of
positions, exactly as it must be with a real sensor that does not measure heading (ADR 0002).

**Association is global, not greedy.** At a track crossing every detection genuinely is closest
to the wrong track, so greedy nearest-neighbour fails there *by construction*. The whole
assignment is solved together, over Mahalanobis distances that weight each axis by the filter's
actual uncertainty along it. That, plus velocity in the state, is what carries identity through a
crossing (ADR 0005).

**Demotion is driven by elapsed time, not by cycle count.** A track is walked through every state
that time has earned, so `coast_seconds` means six seconds whether the pipeline is running at
2 Hz or 0.2 Hz. One step per cycle would have made a slow cycle rate silently keep dead tracks on
the operator's screen.

**Staleness is measured on one clock.** `last_seen` is the sensor's timestamp and is published as
such; `last_supported` is the pipeline's, and is what drives demotion. Subtracting one from the
other gives a duration plus the skew between two machines — which is exactly what it used to do,
and why a sensor 25 s out of sync had its tracks retired on their first missed cycle (FM-012a).

**The tracker owns track state, including fields it never reads.** Classification and risk hand
their answers here rather than keeping their own copies, because every event carries a full
snapshot and a field the tracker does not carry is a field the next `TRACK_UPDATED` erases
(FM-025).

## What is not here

No fusion. Two sensors observing one object currently produce two tracks — that is `backend/fusion`,
which is not built, and FM-023 records what it costs in the meantime.
