# `sensors/adapters/uart_fmcw_radar` — the serial-attached FMCW radar

## Status: testbed hardware, unvalidated

This is a driver for a **placeholder sensor**, and it will be replaced by production-grade
hardware. Nothing it produces has been validated against a real airframe. It is not wired into
the default run: `./start.sh` still starts the simulator, and this adapter only exists in a
process that constructs it explicitly.

Everything in this document is here so that the swap, when it comes, touches one directory.

## Responsibility

One `SensorAdapter` implementation, over a serial link. Framing, byte layout, command
handshake, quantisation, the sensor's own coordinate frame, and its missing elevation channel
all stop here. Downstream of `get_detections()` there is nothing but canonical `Detection`
objects, identical in kind to the simulator's.

## Interface boundary

**Provides:** `UartFmcwRadarAdapter` (a `SensorAdapter`), its configuration models, and
`ByteLink` for anyone replaying a captured byte log.

**Depends on:** `sensors.adapters.interface`, `sensors.schemas.detection`,
`backend.common.clock`, `backend.common.geodesy`, `backend.common.identifiers`. Nothing else,
and in particular nothing in `backend/zones` — the adapter is given a mount position, not a
site.

**Does not:** know what a track is, appear in any downstream `import`, or add a field, a flag
or a side channel to `Detection`. If it ever needs to, the design is wrong (ADR 0002).

## Files

| File | Responsibility |
|---|---|
| `config.py` | Every hardware number, with `UNVERIFIED` marked in the docstring where it is |
| `protocol.py` | Framing and byte-level decoding. Pure — no I/O, no clock, no schema |
| `link.py` | `ByteLink`, and the only `pyserial` import in the repository |
| `adapter.py` | Lifecycle, geometry, uncertainty, health, raw logging, configuration |

## The hardware

| Item | Detail |
|---|---|
| Sensor | Hi-Link HLK-LD2451, 24 GHz FMCW, vehicle-detection firmware |
| Band / modulation | 24–24.25 GHz, FMCW, sweep bandwidth < 200 MHz |
| Range | Rated to 100 m, configurable 10–100 m, **at vehicle-scale radar cross-section** |
| Beam | ±20° azimuth, fixed relative to the PCB. No elevation output of any kind |
| Power | 5 V DC, supply ≥ 300 mA, ~107 mA average |
| Interface | 1× UART at **3.3 V TTL**, 2× GPIO at 3.3 V |
| Temperature | −40 °C to 85 °C |
| Size | 70 mm × 35 mm |
| Link | AYWHP CP2102 HW-598 USB-to-TTL adapter, up to 1 Mbps, native CP2102 driver |
| Vendor config tool | Hi-Link Bluetooth app — manual calibration only, outside this integration |

The model numbers appear in this README and nowhere in the code, which is deliberate: no
manufacturer or product name belongs in a module the rest of Aetos imports.

### Wiring

```
LD2451 radar            CP2102 adapter          Host
------------            --------------          ----
VCC          <------>   5V
GND          <------>   GND
TX           <------>   RX
RX           <------>   TX
                        USB          <------>   USB port
```

**The one way to destroy the sensor.** The module is powered from 5 V, but its UART pins are
3.3 V logic. The CP2102's logic-level switch must be set to **3.3 V**. On 5 V it over-drives
the radar's UART pins. Software cannot detect this and cannot protect against it.

## Constraints, and where each one is answered in code

| Constraint | Where it is handled |
|---|---|
| No elevation channel | `MountConfig.tilt_deg` sets the beam-centre elevation; `MountConfig.elevation_ambiguity_deg` becomes the vertical variance in `position_covariance_enu`, so the filter is told not to believe the altitude (FM-051) |
| ±20° beam, fixed to the PCB | `MountConfig.boresight_deg`; reports outside `azimuth_half_width_deg` are dropped |
| Vehicle-tuned sensitivity | No classification hint is ever emitted, and `ConfidenceConfig` caps confidence well below 1.0 (FM-052) |
| No raw IF or SNR | The signal-quality byte is uncalibrated, so `snr_db` stays `None` and the byte is mapped onto a bounded confidence instead |
| Little-endian frames | `protocol.py`, tested against split reads, noise, and false headers |
| Baud rate unconfirmed | `UartLinkConfig.baud` with `probe_baud_rates`; a stream that never syncs reports DEGRADED naming the baud rate rather than looking quiet (FM-050) |
| Frame carries no timestamp | Detections are stamped at read time from the injected clock — receipt time, documented in `adapter.py` and FM-050 |

## Bring-up, in order

Nothing below is optional, and none of it is detection logic. **Do not write detection or
alerting behaviour against this hardware until step 5 has produced a dataset.**

1. **Confirm the baud rate against the physical unit.** Sources disagree between 115200 and
   256000. Sweep `probe_baud_rates` and watch `health()`: the wrong rate produces
   `consecutive_bytes_discarded` climbing with `frames_decoded` at zero.
2. **Get the vendor's protocol PDF from the seller** and verify the frame structure against it
   rather than against secondhand sources. Then delete whichever branch of
   `parse_report_payload` the PDF rules out, and replace the two-layout tolerance with the one
   layout that is real. That deletion is a task, not an option.
3. **Verify the command acknowledgements.** `configure()` currently sends the handshake and
   does not read the unit's replies back, so a call that returns has been *sent*, not
   confirmed. Reading the acknowledgement is the next piece of driver work.
4. **Replace every `UNVERIFIED` default in `config.py`** with a measured figure, or record why
   it could not be measured.
5. **Run the raw-logging test matrix** (`raw_log_path` set, no detection logic in the loop):

   | Variable | Values |
   |---|---|
   | Target | Bare airframe · airframe with an RCS booster |
   | Range | 10 m · 25 m · 50 m · 100 m |
   | Angle | Boresight · mid-beam · beam edge, both sides |
   | Motion | Hover · direct approach · lateral crossing |
   | Mount tilt | 0° · 20° · one steeper angle |

   `mount_tilt_deg` is a **test variable recorded per session**, never a setup constant. The
   baseline pass is at 0°; tilt is a second pass, once the parser and pipeline are trusted.

6. Only then assess whether a 135 g, 25 cm airframe is detectable at all, and at what range.
   The answer may be "no", and that is a finding rather than a failure of the driver.

## Swap-out day

When production hardware arrives, delete this directory and add the new one beside it.
`sensors/adapters/interface.py`, `sensors/schemas/detection.py`, and everything in tracking,
fusion, classification, risk, decision and alerting stay untouched — provided the new adapter
emits canonical `Detection` objects through the same four methods.

The two tests in `tests/unit/test_uart_fmcw_radar_*.py` go with the directory. Nothing else
in the suite references this hardware, which is the property that makes the swap a deletion
rather than a migration.

## Non-goals

No elevation tracking. No multi-unit fusion here (that is `backend/fusion`, and it is not
built). No signal processing — the hardware exposes no raw IF data, so there is nothing to
process. And no confidence claim about detection performance until step 5 above has produced
real data.
