# `sensors/adapters/uart_fmcw_radar` — testbed hardware adapter

## What this is

A `SensorAdapter` for a low-cost 24 GHz FMCW radar module reachable over a UART-to-USB serial
link. The module reports up to a few targets per frame: angle, distance, signed speed, and a
per-target signal-strength byte.

**This is the current testbed sensor, not the production radar.** It exists so the pipeline in
`backend/` can be exercised against a real, physical detection source while the eventual
production sensor is still an open question. Everything specific to this unit — wire protocol,
mount geometry, calibration placeholders — lives inside this one directory. Swapping the
hardware later means adding a new adapter directory and pointing whatever constructs sensors at
it; nothing in `backend/`, `sensors/schemas/`, or `sensors/adapters/interface.py` has to change,
by the same boundary that lets the simulator and a real sensor coexist today (`docs/adr/0002`).

## Hardware this was written against

- Radar module: 24 GHz FMCW, vehicle-detection firmware, one UART output (3.3V TTL logic),
  rated 10–100 m configurable detection range, ±20° fixed azimuth beam relative to the PCB, no
  native elevation output.
- USB-to-TTL bridge: a common UART-to-USB chip, set to 3.3V logic level to match the radar.

Model numbers and vendor deliberately do not appear in the Python source in this package, or
anywhere under `backend/` or `sensors/` — see `tests/unit/test_module_boundaries.py` and the
"narrow waist" principle in `sensors/adapters/README.md`. They're named here in prose only,
because a future maintainer replacing the hardware needs to know what this adapter was written
against.

## Protocol source

The frame format in `protocol.py` (header/footer byte sequences, payload layout, 5-byte target
record) was reverse-derived from a community open-source reference implementation of the
vendor's serial protocol for this module family, not transcribed directly from the vendor's own
protocol PDF (which was not reachable while writing this). Treat the framing (header, length
field, footer, target-record size) as solid — it round-trips in `tests/unit/` against
hand-built frames — but treat the **semantic** conventions below as assumptions pending field
verification, not facts:

- **Direction sign.** Which value of the direction byte means "approaching" versus "receding"
  is unconfirmed. See `config.RadarMountConfig.direction_zero_means_approaching`.
- **Angle sign.** Which side of boresight is positive is unconfirmed. Lower stakes than
  direction (it only affects which side of the mount a target renders on), so there's no config
  flag for it — flip the sign in `adapter.py`'s `_build_detection` if a field check shows
  targets landing mirrored.

## Known limitations — read before trusting this in anything operational

- **Rated for vehicle-scale RCS, unverified for small airframes.** The firmware and its
  detection thresholds are tuned for cars and motorcycles. Whether a small UAS returns enough
  signal to trigger a detection at any useful range has not been tested. Treat every Detection
  this adapter produces as *what the module reported*, not as *confirmation it can see a
  drone*.
- **No native elevation.** Height is inferred entirely from `RadarMountConfig.tilt_deg`, a
  fixed physical mount angle, not measured per detection — the datasheet is explicit that
  elevation coverage is only achievable by physically tilting the mount.
  `elevation_uncertainty_deg` in `config.py` is deliberately wide to reflect that inference,
  not a genuine angular-resolution spec.
- **No per-detection hardware timestamp.** The module streams continuously and the protocol
  carries no device-side timestamp, so frames are timestamped at receipt (from the injected
  `Clock`), not at the instant the module made the observation. Latency between the two is
  expected to be small at this update rate but has not been measured. See `docs/failure_modes.md`
  FM-012 for why naive timestamp handling matters here generally; this is a hardware limitation
  on top of that, not a bug in how the clock is used.
- **Confidence is a relative proxy, not a calibrated probability.** `Quality.confidence` is a
  linear map of the module's undocumented per-target signal byte, clamped into
  `[confidence_min, confidence_max]`. Useful for comparing detections from this sensor against
  each other; not comparable to a calibrated source's confidence.
- **Never configures the module.** Only data frames (`DATA_FRAME_HEADER` /
  `DATA_FRAME_FOOTER`) are parsed. The module's own configuration (detection range,
  sensitivity) — normally set through its companion Bluetooth app — is out of scope for this
  adapter; set it once, out of band, before running this code. The config-frame constants in
  `protocol.py` exist only so a config-ack frame is recognisable as *not* a data frame, not to
  issue commands.

## Layout

| File | Responsibility |
|---|---|
| `protocol.py` | Pure frame parsing. No I/O, no `pyserial` dependency — testable with hand-built byte sequences. |
| `transport.py` | The only file that touches the OS or a physical port. Wraps `pyserial`, imported lazily so the rest of this package (and its tests) works without it installed. |
| `config.py` | `SerialLinkConfig` (the wire) and `RadarMountConfig` (geometry and calibration placeholders) — kept separate from `config/sensor_registry/`; see `config.py`'s docstring for why. |
| `adapter.py` | `UartFmcwRadarAdapter`, implementing `SensorAdapter`. Wires the above into canonical `Detection` objects. |

## Installing

`pyserial` is an optional dependency, not part of Aetos's core install, so nothing outside this
one package needs it:

```bash
pip install .[hardware]      # or just: pip install pyserial
```

## Wiring it into a running system

This package is deliberately **not** wired into `backend/main.py` yet. Doing so needs a
decision about how a real-sensor placement is configured alongside the simulated ones —
`Site`/`SensorPlacement` today assume a `registry_key` into the simulated registry, and folding
a real adapter's `SerialLinkConfig`/`RadarMountConfig` into that shape is a site-configuration
question, not something this adapter should decide unilaterally. Until that's settled, construct
and drive it directly:

```python
from backend.common.clock import SystemClock
from backend.common.geodesy import EnuPoint, GeodeticPoint, LocalFrame
from sensors.adapters.uart_fmcw_radar import RadarMountConfig, SerialLinkConfig, UartFmcwRadarAdapter

clock = SystemClock()
frame = LocalFrame(GeodeticPoint(latitude_deg=..., longitude_deg=..., altitude_m=...))

adapter = UartFmcwRadarAdapter(
    clock=clock,
    sensor_id="testbed_radar_01",
    position_enu=EnuPoint(east_m=0.0, north_m=0.0, up_m=0.0),  # offset from the site origin
    frame=frame,
    serial_config=SerialLinkConfig(port="/dev/ttyUSB0"),
    mount_config=RadarMountConfig(boresight_deg=0.0),
)
adapter.start()
detections = adapter.get_detections()  # poll on whatever cadence the caller wants
```

`get_detections()` never blocks waiting for hardware and never raises for a disconnected or
unplugged sensor — it returns `[]`, retries the connection on `serial_config.reconnect_interval_s`,
and the fault is visible in `adapter.health()`, per the `SensorAdapter` contract.

## Before this becomes more than a testbed

- Run a real calibration pass (known ranges and bearings) and replace the placeholder sigmas in
  `RadarMountConfig` with measured ones.
- Confirm the direction and angle sign conventions in the field (see "Protocol source" above)
  rather than trusting the assumed defaults.
- Decide, deliberately, how a real-sensor placement is expressed in site configuration, then
  wire this adapter into `backend/main.py`'s `Pipeline` the same way `SimulatedSensorAdapter`
  is wired in today.
