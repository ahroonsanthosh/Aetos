# `sensors/radar` — physical radar units, behind a contract that outlives them

## Responsibility

Talking to a radar over a wire. Serial link management, frame decoding, configuration commands,
raw logging, and self-reported health — expressed through one interface, `RadarSensor`, so that
the unit behind it can be replaced without touching anything else.

This is a **second, inner boundary**, one level below `sensors/adapters/`. The outer boundary
makes the simulator and a real sensor indistinguishable to Aetos. This one exists for a narrower
reason: the unit currently on the bench is explicitly a testbed, it will be replaced, and
replacing it must be a two-file change (ADR 0017).

```
hardware  ->  RadarSensor  ->  HardwareRadarAdapter  ->  Detection  ->  Aetos
              (here)           (sensors/adapters/         (canonical)
                                hardware_radar/)
```

## Interface boundary

**Provides:** `RadarSensor` (`connect`, `disconnect`, `get_targets`, `get_status`, `configure`),
`TargetDetection`, `SensorStatus`, `RadarConnectionState`, `RadarSensorError`, `RadarConfig` and
its loader, and the beam geometry in `geometry.py`.

**Depends on:** `backend.common.clock` for time, `backend.common.config` for document loading and
path resolution, `backend.common.geodesy` for the ENU point type. Nothing else.

**Does not:** know what a Detection is, what a site is, what WGS-84 is, or where it is mounted in
the world. It reports ranges and angles in the unit's own frame and stops there.

## Why `TargetDetection` is not `Detection`

A radar measures a range, an angle off its own boresight, and a range rate. Turning that into a
WGS-84 position with an ellipsoidal altitude needs the mount geometry and the site's local frame,
and minting a detection identifier needs a generator scoped to the sensor. Pushing all of that
into the driver would make every future driver reimplement geodesy — and, worse, would put the
decision of *what altitude to report* inside the component least equipped to answer it honestly.

For the current unit the correct answer is "I do not know, and here is how much I do not know".
That is a statement about uncertainty, and it belongs where the covariance is assembled: the
adapter.

## Replacing the hardware

Two files: the protocol codec and the driver built on it. `config.py` gains or loses fields.
`geometry.py`, `transport.py`, `raw_log.py`, the adapter and everything downstream are untouched.

Two tests enforce this rather than trusting it — `tests/unit/test_module_boundaries.py` fails the
build if the model designation appears outside those two modules, or if the adapter imports a
concrete driver.

## Sub-modules

| Path | Responsibility |
|---|---|
| `radar_interface.py` | The contract, and the data that crosses it |
| `config.py` | Link, tuning, mount, uncertainty and raw-logging configuration |
| `geometry.py` | Mount tilt and bearing into an ENU offset with an honest covariance |
| `transport.py` | The byte pipe, behind an interface so drivers are testable without hardware |
| `raw_log.py` | Every frame, with timestamps and the session's mount tilt |
| `ld2451_protocol.py` | **Model-specific.** Frame layout, target decoding, command words |
| `ld2451_driver.py` | **Model-specific.** Link management, polling, configuration, health |

## Three things this layer is careful about

**Silence is never success.** An empty target list means the radar reported nothing. A closed
port, a failed link, and a byte stream that will not frame are all visible in `get_status()`, and
polling before `connect()` raises. A driver that returned `[]` for all of those would make a dead
radar and a quiet sky identical (FM-010).

**The protocol constants are unverified.** They come from secondhand descriptions of the vendor
document, not the document itself. The decoder therefore counts every byte it discards and every
frame it rejects rather than repairing either, so a wrong assumption produces a distinctive
signature instead of plausible-looking targets at nonsense ranges. See the checklist in
`docs/radar_testbed.md`.

**Time comes from the injected clock.** Including the reconnect backoff and the
command-reply timeout, which are bounded by attempt count as well as by elapsed time so that
they terminate under a `SimClock` as well as a `SystemClock` (ADR 0003).

## Hardware dependency

`pyserial`, optional: `pip install -e ".[hardware]"`. It is imported inside `open()`, so
importing this package never requires it and the test suite runs without it.
