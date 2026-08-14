# `sensors/adapters/radar` — radar sources

## Responsibility

Radar adapters, and the only place in the codebase permitted to know anything about a
particular radar unit: its model, its transport, its framing, its native units. Everything
downstream reads `SensorAdapter` and canonical `Detection`s, so nothing outside this directory
knows a radar has a serial port or a product name.

Enforced by `tests/unit/test_module_boundaries.py`, not by convention.

## Contents

| File | Status |
|---|---|
| `stub_radar_adapter.py` | **Active.** Fabricated returns, no sensor, no truth source. |
| `ld2451_radar_adapter.py` | **Empty shell.** Every method raises `NotImplementedError`. |
| `ld2451_protocol.py` | **Done and tested.** Byte stream in, target reports out. |

## What is already finished

`ld2451_protocol.py` is the wire format, built and tested before the hardware exists. Framing a
byte stream is where serial drivers actually fail — not in the arithmetic but in
resynchronising after a partial read, a dropped byte, or a payload that happens to spell the
header — and all of that is testable against synthesised bytes. 23 tests cover resync after
garbage, frames arriving one byte at a time, a header sequence inside a payload, a corrupt
length field that would otherwise stall the reader forever, and a bounded buffer so a wrong
baud rate cannot exhaust memory.

What those tests do **not** cover is whether the byte-level constants match the device. They
synthesise frames with the same `WireFormat` they parse with, so they prove the algorithm and
say nothing about the values. Every constant is unverified; `docs/radar_bringup.md` is the
procedure that settles them, and `WireFormat` is a value rather than scattered literals so that
correcting one is a data change with every test still applying.

## The stub is not the simulator

`StubRadarAdapter` draws returns from configured ranges and corresponds to nothing. It exists
so the ingestion path, the tracker and the operator interface can be exercised before hardware
arrives, and it reports `SIMULATED_RADAR` so a bench recording can never be mistaken for a live
one.

`sensors/adapters/simulated/` is the thing to use for any question about tracker or fusion
behaviour: it degrades scenario ground truth through a modelled sensor, so its output is wrong
in the specific ways a real radar is wrong. The stub's output is uncorrelated noise, and any
conclusion drawn from it about association, fusion or scoring is worthless.

## When the physical radar arrives

Fill in `ld2451_radar_adapter.py`. Its module docstring is the specification: transport,
framing and resynchronisation, the per-target conversions, the anisotropic covariance, the
sign convention on radial velocity, and what has to be checked against a surveyed reference
before the output is trusted.

Then, in `sensors/live.py` — the composition point, and the only file in the repository that
names a concrete adapter class — change the marked line:

```python
adapter=StubRadarAdapter(   ->   adapter=Ld2451RadarAdapter(
```

and its import. Delete `stub_radar_adapter.py`. Nothing else changes: `backend/main.py` builds
whatever that file returns, and its polling loop is typed to `SensorAdapter`.

This is verified rather than asserted. `tests/unit/test_module_boundaries.py` fails the build
if any module other than `sensors/live.py` names a concrete adapter, and
`tests/integration/test_live_pipeline.py` drives the whole path — adapters through ingestion
and tracking to a track — without naming a camera or a radar anywhere, so it keeps passing
across the swap and fails immediately if the swap breaks the path.

## Configuration

`sensors/config/sensor_config.yaml`, under `radar:`. Mounting position, boresight and sensor
ID apply to any radar adapter; the `radar.stub:` block is read by the stub alone and goes with
it. A real unit adds a `radar.serial:` block rather than hard-coding a port.
