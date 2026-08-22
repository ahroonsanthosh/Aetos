# `sensors/adapters` — the source boundary

## Responsibility

One interface, `SensorAdapter`, implemented by every source of detections. Everything a
particular sensor is peculiar about stops here: protocol, polling versus streaming, native
coordinate frame, altitude datum, units, and clock discipline.

Together with the canonical `Detection` schema this is the narrow waist of Aetos. It is what
makes the governing principle enforceable — **the tracking engine cannot tell whether a
detection came from the simulator or from a physical sensor** (`docs/architecture.md`).

## Interface boundary

**Provides:** `SensorAdapter` (`start`, `stop`, `get_detections`, `health`, `capabilities`),
`SensorHealth`, `SensorState`, `SensorAdapterError`, `SensorCapabilities` and the floors it is
measured against.

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

## What a source declares about itself

`health()` says what a sensor is *doing*. `capabilities()` says what it **is**: coverage,
timing, accuracy, which dimensions it genuinely measures, and how trustworthy the link carrying
its data is. Static, cheap, and available before the adapter is started — which is when a
customer wants to see it.

The two are separate on purpose. Folding capability into health would make a declared
specification appear to change when a cable came loose.

That declaration is what the floors are measured against, and the floors are **computed** from
the deployment's own tracking tuning by `backend/tracking/requirements.py` rather than written
down here. A number in a second place is a number that will stop agreeing with the first.

The governing idea is worth stating plainly, because it decides what a customer has to do:

> Aetos does not require a good sensor. It requires a sensor that tells the truth about how
> good it is.

An honest 15-metre error is usable — the gate widens, the filter weights it lightly, the
operator sees a larger ellipse. A source claiming two metres and delivering fifteen is unusable
at any accuracy, because the filter believes it. See `docs/sensor_interface_specification.md`
and ADR 0017.

## Health

`health()` reports facts — state, last detection time, count, and a reason in words — and
makes no judgement about whether a quiet sensor is broken. Whether silence means a fault or an
empty sky is a site-level policy decision, and it belongs to `backend/system_health`, which
polls this record. The threshold that is right for a 10 Hz imaging sensor is wrong for a 1 Hz
radio-frequency one, so the adapter does not pretend to know it (FM-010).

## Adding a real adapter

A new source is a new sub-package here plus a registry entry, and nothing else. None of
tracking, fusion, classification or risk names a sensor kind or reads anything but a
`Detection`.

1. Write a registry document in `config/sensor_registry/` with `kind: "live"`.
2. Implement `SensorAdapter` in `sensors/adapters/<driver>/`. Keep the wire codec a pure
   function of bytes with no I/O, so it can be tested exhaustively without the hardware.
3. `register_driver(name, builder)` so the factory can resolve it.
4. Run the conformance suite. Fix what it names.
5. Measure what the sensor can actually see, and only then set `coverage_validated` (FM-016).

**Vendor and model names live inside one driver package and nowhere else.**
`tests/unit/test_module_boundaries.py` fails the build if one appears elsewhere in `backend/` or
`sensors/`. A driver naming its hardware is what a driver is; `backend/risk` acquiring a
threshold that only makes sense for one radar is the failure being prevented.

No live driver is registered today. LIVE and HYBRID therefore refuse to start rather than
falling back to the simulator, and the reserved `radar` / `eo_ir` / `rf` / `acoustic` values in
`SensorType` remain a schema reservation rather than a capability claim.

## Sub-modules

| Path | Responsibility |
|---|---|
| `interface.py` | The contract |
| `capabilities.py` | What a source declares, and the floors it is measured against |
| `registry.py` | Loading `config/sensor_registry/`, both simulated and live entries |
| `factory.py` | Placement plus registry entry to adapter; which run mode builds what |
| `conformance/` | The runnable suite any adapter is measured with |
| `simulated/` | The simulated adapter and its sensor model |
| `ld2451/` | One device's wire codec. Contained: the name appears nowhere else |
