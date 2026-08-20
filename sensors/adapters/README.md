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

An adapter for real hardware is an addition to this directory and nothing else. None of
tracking, fusion, classification or risk names a sensor kind or reads anything but a
`Detection`, so a new source changes no module downstream — `radar/` was added without one
line changing in any of them, which is the claim this architecture makes and the first time it
was actually tested.

`radar/` reads a short-range serial radar module and is the only real adapter that exists;
`eo_ir`, `rf` and `acoustic` remain schema reservations with no implementation. What the fitted
device can and cannot do, and what it is honest to claim from it, is in
`docs/hardware_radar_bench.md`.

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
| `radar/` | A physically fitted serial radar: framing, transport, mounting, and its adapter |
