# `sensors/adapters/ld2451` — one 24GHz FMCW ranging module

## Responsibility

Everything Aetos knows about this specific device. Its wire format, its quirks, its limits and
its model number live here and **nowhere else** —
`tests/unit/test_module_boundaries.py::test_vendor_names_are_confined_to_their_own_driver_package`
fails the build if the name appears anywhere else in `backend/` or `sensors/`.

That containment is the point. This hardware will be replaced, and when it is, the cost should
be this directory and nothing beyond it. Nothing downstream of `SensorAdapter` can name it, so
nothing downstream needs to change.

## What exists, and what does not

| | Status |
|---|---|
| `protocol.py` — wire codec and integrity accounting | **built and tested** |
| `transport.py` — the serial port, behind a fakeable seam | **built and tested** |
| `adapter.py` — `SensorAdapter`: time, geometry and trust | **built and tested** |
| `bench.py` — hardware diagnostics | **built** |
| Registry entry `config/sensor_registry/ld2451_uart.json` | **live**; set `port` and `azimuth_sign` for your unit |
| Measured coverage against the threat class | **not done.** `coverage_validated` is false — `BENCH.md` step 9 |

The codec was written first, deliberately, and is a pure function of bytes with no I/O at all.
That is what lets it be tested exhaustively — against truncation, desynchronisation, byte-level
corruption and garbage — with no hardware present. A parser you can only test against a live
device is a parser you cannot test.

Everything except the last row can be exercised without the device: `FakeTransport` scripts a
link, and `tests/integration/test_live_radar.py` runs the whole apparatus — factory, conformance
suite, adapter, tracker — over one. The last row cannot, and that is the point of it.

## Running it

```bash
python -m sensors.adapters.ld2451.bench --list                    # find the port
python -m sensors.adapters.ld2451.bench --port /dev/ttyUSB0       # talk to the device
python -m sensors.adapters.conformance --site bench               # check the declaration
python -m backend.main --site bench --mode live --no-scenario     # run the system on it
```

**`BENCH.md` is the procedure**, in order, from an unopened box to a measured coverage figure.
Step 6 — calibrating the azimuth sign — is the one that silently mirrors the whole picture if
skipped.

## The frame

Little-endian throughout.

```
F4 F3 F2 F1 | length (uint16) | count | alarm | target * N | F8 F7 F6 F5
                                                └─ 5 bytes each ─┘

target:  angle | distance | direction | speed | snr
         │       │          │           │       └─ 0..100
         │       │          │           └───────── whole km/h
         │       │          └───────────────────── 01 approaching, 00 receding
         │       └──────────────────────────────── whole metres
         └──────────────────────────────────────── degrees, offset by 0x80
```

`length` counts the payload only, so `length == 5N + 2`.

Default UART is 115200, 8N1. The rate is persistent across power cycles and configurable up to
460800, which is why both 115200 and 256000 appear in the wild — the driver must probe rather
than assume, and say which rate worked.

## Three constraints that shape everything here

### There is no checksum

Not a weak one — none. Corruption that changes length or alignment is caught by the
length-versus-tail agreement; corruption that changes a payload byte in place is caught by
**nothing in the protocol**. A single bit flip turns a target at 40 m into one at 168 m in a
frame that is structurally perfect.

So the plausibility envelope is not defensive tidiness. On this protocol it *is* the integrity
check, and the probability of it letting corruption through is computed rather than assumed:
`residual_corruption_probability` derives it from the frame layout and the configured bounds,
and an exhaustive test verifies the computation against what the decoder actually accepts.

For the shipped configuration that residual is **0.209** — about four in five single-byte
corruptions are caught. Tightening the speed ceiling is the single most effective way to improve
it, because speed has the widest legal code space of any field. See ADR 0018.

**Physical mitigations dominate and are worth more than any of this:** short shielded leads, a
solid common ground, distance from motors and switching supplies, and the *lowest* baud rate
that still meets the update-rate floor.

### There is no elevation channel

The device measures range and azimuth. It cannot say how high anything is.

The canonical schema requires an altitude, so the adapter assumes the target lies in the
sensor's own horizontal plane and marks the figure `assumed` (ADR 0019).
`assumed_altitude_sigma_m` computes the vertical uncertainty that assumption carries — uniform
over the beam's vertical extent at that range, so half-width over root three. At 100 m with a
30° aperture that is about 15 m, which correctly tells the filter it has learned almost nothing
about altitude.

### The detection threshold is tuned for vehicles

The rated range is quoted against a car-scale radar cross-section. A small multirotor is one to
two orders of magnitude below that, and **whether this device detects one at all, at any range,
is unknown.**

Every figure in the registry entry marked unconfirmed is a datasheet claim or an estimate, and
`coverage_validated` is false. Until someone has flown the actual airframe at known ranges and
recorded detection probability against range, silence from this sensor must not be rendered as
clear sky (FM-016).

## Files

| File | Responsibility |
|---|---|
| `protocol.py` | Frame codec, plausibility envelope, integrity accounting, assumed-altitude uncertainty |
| `transport.py` | The serial port behind a `Protocol`, plus the fake that makes the rest testable |
| `config.py` | Driver parameters, and the invariants that keep the adapter inside its own declaration |
| `link.py` | Sliding-window link quality — the evidence the sensor is degraded on |
| `adapter.py` | Reader thread, observation time, geometry, covariance, health |
| `bench.py` | Hardware diagnostics: port discovery, baud probe, live decode, raw recording |
| `BENCH.md` | The bench procedure |

## Related

- `docs/adr/0018-transport-integrity.md` — why a checksumless link is measured rather than trusted
- `docs/adr/0019-assumed-altitude.md` — what a sensor with no elevation channel reports
- `docs/adr/0020-live-adapter-threading-and-time.md` — the reader thread, observation time, and
  the one-instant-per-cycle rule this device's update rate broke
- `docs/sensor_interface_specification.md` — the floor this device is measured against
- `tests/unit/test_ld2451_protocol.py` — the codec's proof, including the exhaustive corruption test
