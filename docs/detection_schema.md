# Canonical Detection schema

**Current version: 1.0.0** · Defined in `sensors/schemas/detection.py`

This is the contract every sensor source must satisfy — the simulator today, any real sensor
adapter in future. It is the narrow waist of the system: nothing downstream of ingestion can
tell which kind of source produced a given detection.

Living document. Changes here are changes to the contract.

## What a Detection is

> A sensor observed something at this location at this time.

One raw observation, by one source, at one instant. It is **not** a belief about an object's
identity or continuity — that is a Track, and forming Tracks from Detections is the tracking
engine's job. See spec §3.

A Detection is **immutable**. It is a historical fact; correcting one means emitting a new
one, never mutating the original.

## Versioning

Every Detection carries `schema_version`. Minor version bumps for backwards-compatible
additions, major for breaking changes. An adapter written against an older version remains
identifiable rather than silently misinterpreted.

`extra="forbid"` is set on every model: a source emitting a field we do not recognise is
rejected rather than silently ignored, because silent tolerance hides an adapter written
against the wrong version until it causes a subtler problem.

## Units and datums — stated once, applied everywhere

| Quantity | Unit | Notes |
|---|---|---|
| Latitude, longitude | degrees, WGS-84 | |
| **Altitude** | **metres above the WGS-84 ellipsoid** | **Not AGL. Not MSL. Not barometric.** |
| Distance | metres | |
| Speed | metres per second | |
| Bearings and headings | degrees, clockwise from true north, `[0, 360)` | 360 rejected as a duplicate encoding of 0 |
| Time | UTC, ISO-8601, millisecond precision | timezone-aware always |

The altitude datum deserves the emphasis. Real sensors variously report height above mean sea
level, height above ground level, or barometric altitude, and confusing them is a silent
error of tens to hundreds of metres. Converting to the canonical datum is the **adapter's**
documented obligation. See `failure_modes.md` FM-004.

Naive timestamps are rejected at the schema boundary. An ambiguous timestamp in a
multi-sensor system produces velocity estimates wrong by whatever the timezone offset happens
to be, which is far cheaper to reject than to diagnose. See FM-012.

## Fields

```json
{
  "schema_version": "1.0.0",
  "detection_id": "det-simradar01-000123",
  "timestamp": "2026-08-08T12:00:01.250Z",
  "sensor": {
    "sensor_id": "SIM-RADAR-01",
    "sensor_type": "simulated_radar"
  },
  "position": {
    "latitude": 53.3508,
    "longitude": -6.2603,
    "altitude_m": 120.0
  },
  "measurement": {
    "position_uncertainty_m": 15.0,
    "position_covariance_enu": null
  },
  "quality": {
    "confidence": 0.87,
    "snr_db": null
  },
  "kinematics": {
    "speed_mps": 32.0,
    "heading_deg": 185.0,
    "vertical_rate_mps": null,
    "radial_velocity_mps": -14.0,
    "provenance": "derived"
  },
  "classification": {
    "label": "unknown_uas",
    "confidence": 0.61,
    "provenance": "measured"
  }
}
```

### Required

| Field | Meaning |
|---|---|
| `schema_version` | Version this detection was produced under |
| `detection_id` | Unique within a run; deterministic and sensor-scoped |
| `timestamp` | When the observation was **made**, not received or processed |
| `sensor` | `sensor_id` and generic `sensor_type` — never a manufacturer or model |
| `position` | WGS-84, ellipsoidal altitude |
| `measurement` | Positional uncertainty |
| `quality.confidence` | Source's confidence this is a real object rather than noise |

### Optional — and this is the important part

`kinematics` and `classification` are **optional and provenance-marked**. The reasoning is
the most consequential architectural decision in the project, recorded in
[ADR 0002](adr/0002-detection-schema-provenance.md). In short:

Most real sensors do not measure speed or heading. A radar measures range, azimuth,
elevation and sometimes *radial* Doppler; it does not measure heading. Speed and heading are
normally quantities a **tracker derives** from a sequence of positions.

The simulator, by contrast, knows the truth exactly. Left unchecked it would populate these
fields perfectly, the tracker would consume them because they are there and accurate, every
test would pass — and the failure would only surface the day a real sensor was connected.

So:

1. **The V0 tracker does not consume `kinematics` for state estimation**, whatever its
   provenance. Velocity comes from the Kalman filter, estimated from positions.
2. **Detection-level `classification` is evidence, not a verdict.** The classification module
   weights it as one input among several, discounted by provenance, and must produce a
   defensible label without it.
3. **A guard test enforces both.** `tests/integration/test_detection_provenance_guard.py`
   runs a scenario twice — once normally, once with every detection passed through
   `Detection.without_inferred_fields()` — and requires identical track output.

Point 3 is the part that actually holds. The first two are conventions.

### Provenance values

| Value | Meaning |
|---|---|
| `measured` | The sensor directly measured this quantity |
| `derived` | The adapter computed it from that sensor's own measurements |
| `simulated_truth` | Simulator ground truth. **Never authoritative.** |

`simulated_truth` exists so the difference between "the sensor measured this" and "the
simulator knows this because it created the object" is explicit in the data rather than
implied by which code path produced it.

### `radial_velocity_mps` is carried separately, deliberately

Unlike speed and heading, range rate **is** directly measurable by a Doppler-capable sensor.
It sits in the same block but as its own field so that a future ADR can admit it into the
filter as a genuine measurement — gated on provenance `measured`, with its own uncertainty —
without also admitting the derived fields alongside it.

### `position_covariance_enu` — why the scalar is not enough

`position_uncertainty_m` is a single isotropic one-sigma value, required by v1.0. Real sensor
uncertainty is usually strongly **anisotropic**: a radar is precise in range and imprecise in
cross-range, sometimes by an order of magnitude. A scalar cannot express that, and forcing an
isotropic assumption into the filter degrades exactly the discrimination that keeps identities
apart at a track crossing.

Sources able to report a full 3×3 covariance in ENU metres² should. The filter uses it in
preference to the scalar when present, and builds an isotropic matrix from the scalar when it
is not.

## Sensor types

Deliberately generic. No manufacturer, model or product name appears in this enum or anywhere
else in the codebase, and a test asserts it.

| Simulated | Real |
|---|---|
| `simulated_radar` | `radar` — one adapter, a short-range serial module (`sensors/adapters/radar/`) |
| `simulated_eo_ir` | `eo_ir` — reserved, no implementation |
| `simulated_rf` | `rf` — reserved, no implementation |
| `simulated_acoustic` | `acoustic` — reserved, no implementation |

Three of the four right-hand values remain a **schema reservation, not a capability claim**.
The fourth is real: `radar` is produced by the serial adapter described in
`docs/hardware_radar_bench.md`, and it is what `GET /system/mode` counts when it reports
whether anything on screen is a measurement. A sensor's own `sensor_type` is the *only*
channel carrying simulated-versus-real; provenance is not (ADR 0012), and neither is anything
about how a detection looks.

## Classification labels

`unknown` · `unknown_uas` · `small_uas` · `medium_uas` · `large_uas` · `bird` · `vehicle` ·
`other`

Confidence is always reported separately from the label, never folded into it.

## What an adapter must guarantee

Any adapter — simulated or real — takes on these obligations:

1. Emit only valid canonical Detections. Validation failures are the adapter's problem, not
   the pipeline's.
2. Convert to the canonical altitude datum (ellipsoidal), coordinate system (WGS-84) and
   units (SI).
3. Timestamp with when the observation was **made**, timezone-aware.
4. Report uncertainty honestly. An adapter that understates uncertainty makes the filter
   overconfident, which is worse than reporting nothing.
5. Mark provenance truthfully on any inferred field.
6. Never emit ground truth as though it were measurement. For the simulator this means
   degrading truth through the configured sensor model — noise, probability of detection,
   classification confusion — before emitting. Emitting clean truth is a bug, not a shortcut.
