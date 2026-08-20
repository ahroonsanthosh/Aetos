# `sensors/adapters/radar` — a physically fitted radar

## Responsibility

Read a short-range 24 GHz FMCW radar module over a serial link and emit canonical
`Detection`s. The first source in Aetos that is not a simulation.

Everything the device is peculiar about stops in this directory. Nothing downstream of
ingestion changed to accommodate it, and nothing downstream can tell its detections from the
simulator's except by reading `SensorRef.sensor_type`.

| File | Responsibility |
|---|---|
| `protocol.py` | Framing and target decoding. Bytes in, device-unit target reports out. No I/O. |
| `transport.py` | Where the bytes come from: a serial port, or a captured sequence for tests |
| `config.py` | What this fitted sensor *is*: link, mounting, coverage, error figures |
| `adapter.py` | The `SensorAdapter`: reader thread, geometry, uncertainty, health |

## What the device measures

Per target: azimuth relative to boresight, slant range to the metre, a direction flag, a speed
to the km/h, and a unitless signal level.

## What it does not

**Elevation.** There is none — not coarse, not ambiguous, none. Detections are placed at the
centre of the beam at the configured mount tilt, with a vertical uncertainty derived from the
beam's full width. That uncertainty is large on purpose: the object is somewhere in the beam
and the adapter does not know where, and the covariance is where it says so.

**Classification.** The firmware is tuned for vehicles; a target is a moving reflector, not an
identified one. The adapter emits no `ClassificationHint` rather than asserting `vehicle`,
because that label would be weighted as evidence for something the device never determined.

**Any verified performance against a small airframe.** The rated range is for vehicle-scale
radar cross-sections. What this device does with a small UAS is unknown until measured, and
`docs/hardware_radar_bench.md` is how you measure it.

## Two sign conventions are configuration, not constants

Which direction code means *approaching*, and which way the azimuth sign runs. Published
descriptions disagree, and both are wrong in ways that look entirely plausible on a display —
a receding target reported as closing, or a track on the wrong side of the site. Both are in
`config/hardware/*.json`, both are determined in under a minute on the bench, and both have
tests that fail if anyone quietly turns them back into constants.

## Testing without the hardware

The adapter reads a `ByteTransport`, so every test drives it from captured bytes:
`tests/unit/test_radar_protocol.py` covers framing and resynchronisation,
`tests/unit/test_radar_adapter.py` covers geometry, uncertainty and health, and
`tests/integration/test_live_radar_pipeline.py` drives the whole pipeline to a track with no
serial port present. A hardware integration that can only be tested with the hardware attached
stops being tested.

## Threading

A background thread reads the port and hands decoded reports across a bounded queue;
`get_detections()` drains it on the caller's thread, which is also the only place identifiers
are assigned. The queue drops its oldest entry when full and counts every drop — unbounded
would be a memory leak, silent would be a lie.
