# `sensors/adapters` — the source boundary

## Responsibility

One interface, `SensorAdapter`, implemented by every source of detections. Everything a
particular sensor is peculiar about stops here: protocol, polling versus streaming, native
coordinate frame, altitude datum, units, and clock discipline.

Together with the canonical `Detection` schema this is the narrow waist of Aetos. It is what
makes the governing principle enforceable — **the tracking engine cannot tell whether a
detection came from the simulator or from a physical sensor** (`docs/architecture.md`).

## Interface boundary

**Provides:** `SensorAdapter` (`start`, `stop`, `get_detections`, `health`), `SensorHealth`,
`SensorState`, `SensorAdapterError`.

**Depends on:** `sensors.schemas.detection` for the output contract and
`backend.common.clock` for time. Nothing else.

**Does not:** know what a track is, what a site is, or what a scenario is. An adapter reports
what it observed; deciding what those observations mean belongs downstream of ingestion.

`get_detections()` returns canonical `Detection` objects and nothing else — no side channel,
no extra tuple element, no attribute smuggled onto the object. The moment a consumer can read
something only one kind of source provides, the interchangeability the architecture rests on
is gone.

## What an adapter takes on

The full list, with reasoning, is in `docs/detection_schema.md` ("What an adapter must
guarantee") and restated on the `SensorAdapter` docstring. In brief:

1. Emit only valid canonical Detections.
2. Convert to the canonical datum (metres above the WGS-84 ellipsoid), frame and SI units.
3. Timestamp with when the observation was **made**, timezone-aware, from the injected clock.
4. Report uncertainty honestly, including `position_covariance_enu` where the sensor's error
   is anisotropic.
5. Mark provenance truthfully on `kinematics` and `classification`.
6. Never emit ground truth as though it were measurement.

## Adding a real adapter

A real adapter is an addition to this directory and nothing else. None of tracking, fusion,
classification or risk names a sensor kind or reads anything but a `Detection`, so a new source
changes no module downstream.

`uart_fmcw_radar/` is the first one, and it is the test of that claim: adding it changed no
module downstream of ingestion and no existing test. It is a driver for **testbed hardware**,
built to be deleted — unvalidated, wired into no run mode, and described in full by its own
README and ADR 0017. Run mode LIVE remains not built.

What that adapter had to do, and what any real one will have to do, is say what its hardware
cannot support rather than filling the gap: no elevation channel means an honest vertical
covariance instead of a confident altitude (FM-051), vehicle-tuned firmware means no
classification hint at all (FM-052), and an uncalibrated quality byte means no `snr_db`.

The `eo_ir` / `rf` / `acoustic` values in `SensorType` remain schema reservations rather than
capability claims: no adapter exists for any of them.

## Health

`health()` reports facts — state, last detection time, count, and a reason in words — and
makes no judgement about whether a quiet sensor is broken. Whether silence means a fault or an
empty sky is a site-level policy decision, and it belongs to `backend/system_health`, which
polls this record. The threshold that is right for a 10 Hz imaging sensor is wrong for a 1 Hz
radio-frequency one, so the adapter does not pretend to know it (FM-010).

## Sub-modules

| Path | Responsibility |
|---|---|
| `interface.py` | The contract |
| `simulated/` | The simulated adapter, its sensor model, and the registry loader |
| `uart_fmcw_radar/` | Testbed radar over a serial link. Isolated so it can be deleted whole |
