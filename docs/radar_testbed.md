# The radar testbed: hardware, wiring, and what to do before trusting a detection

This describes the physical radar unit currently on the bench, how it is wired, and the campaign
that has to be run before any detection logic is built on it.

**This is not the production radar.** It was bought so that serial framing, reconnection, mount
geometry, honest uncertainty and health reporting would be real problems solved against real
hardware rather than assumptions discovered later. It will be replaced. See ADR 0017 for how the
software is arranged so that replacing it is a two-file change.

---

## 1. Hardware

### 1.1 Radar unit

| | |
|---|---|
| Type | 24 GHz FMCW, vehicle-detection firmware |
| Frequency | 24.00–24.25 GHz |
| Sweep bandwidth | < 200 MHz |
| Detection range | rated to 100 m, configurable 10–100 m — **rated for vehicle-scale radar cross-section, unverified for small airframes** |
| Detection angle | ±20° azimuth, fixed relative to the board. **No elevation output.** |
| Power | 5 V DC, supply capacity > 300 mA, ~107 mA average |
| Interface | 1× UART at 3.3 V TTL, 2× GPIO at 3.3 V |
| Operating temperature | −40 °C to 85 °C |
| Dimensions | 70 mm × 35 mm |

A vendor Bluetooth app can adjust detection distance and sensitivity. It is not needed —
`RadarSensor.configure()` sets the same parameters over the serial link — but it is a useful
cross-check during a manual calibration pass.

### 1.2 USB-to-TTL adapter

CP2102-based, logic level switchable between 3.3 V and 5 V, up to 1 Mbps. The CP2102 driver is
preinstalled on Linux and macOS; Windows may need it installed.

### 1.3 Wiring

```
Radar                CP2102 adapter            Host
-----                --------------            ----
VCC     <-------->   5V
GND     <-------->   GND
TX      <-------->   RX
RX      <-------->   TX
                     USB          <-------->   USB port
```

**Two wiring facts, both easy to get wrong and both silent when wrong:**

- The radar's **VCC always needs 5 V**, whatever the adapter's logic-level switch is set to.
- The adapter's **TX/RX logic level switch must be on 3.3 V** to match the radar's UART. Running
  it at 5 V risks the radar's input and, short of damage, produces a link that opens cleanly and
  never frames.

If the link opens and nothing decodes, check the switch before checking anything in software.

---

## 2. Behavioural constraints the software is designed around

These are not bugs to be worked around. They are properties of the hardware, and the integration
is shaped to state them rather than hide them.

| Constraint | How the software handles it |
|---|---|
| No elevation channel | `TargetDetection.elevation_deg` is `None`. The adapter reports a covariance whose vertical term is the whole beam extent, so the tracker weights altitude as weakly as it deserves. Never filled in with a zero. |
| Narrow beam (±20°) | `mount.azimuth_extent_deg` is configuration. Nothing assumes omnidirectional coverage. |
| Vehicle-tuned detection threshold | `sensitivity` and `min_confirm_frames` are exposed through `configure()` and expected to be swept. Nothing is hardcoded. |
| No raw IF or SNR data | The firmware reports processed targets only. No custom signal processing is possible, and none is attempted. The signal-strength byte is uncalibrated and is not converted into a confidence. |
| Little-endian frames | Every multi-byte decode writes `int.from_bytes(..., "little")` out in full, at the site, so the assumption is visible wherever it is made. |
| **Baud rate unconfirmed** | Sources conflict: 115200 versus 256000. `probe_on_connect` tries the candidates and reports which one framed. Confirm against the unit and write the answer into configuration. |

---

## 3. Software layout

```
sensors/radar/
  radar_interface.py     the RadarSensor contract, TargetDetection, SensorStatus
  ld2451_protocol.py     frame layout, target decoding, command words   <- replaced on swap
  ld2451_driver.py       link management, polling, configuration, health <- replaced on swap
  geometry.py            mount tilt and bearing -> ENU offset + covariance
  transport.py           the serial byte pipe, behind an interface
  raw_log.py             every frame, with timestamps
  config.py              link, tuning, mount, uncertainty, raw logging

sensors/adapters/hardware_radar/
  adapter.py             RadarSensor -> canonical Detection

config/radar/
  testbed_radar.json     this unit's configuration
```

Only the two files marked above are replaced when the hardware changes. That is enforced by
`tests/unit/test_module_boundaries.py`, not merely intended.

---

## 4. First-run checklist

Work through this in order. Nothing after step 5 is meaningful until steps 1–4 are done.

**1. Confirm the baud rate.** Set `link.probe_on_connect` to `true` and connect. The driver tries
each candidate and reports the one that produced a decodable frame. Write that value into
`link.baud_rate` and turn probing back off — a link that needs probing every start is a link
whose configuration is wrong.

**2. Verify the protocol constants against the vendor document.** Obtain the official protocol
PDF from the seller. Check the frame header, length field, tail, command words and target block
layout against the constants at the top of `sensors/radar/ld2451_protocol.py`. They are gathered
there, and flagged as unverified, so that this check is reading one screen rather than auditing a
parser.

**3. Confirm the wiring.** Adapter logic level on 3.3 V; radar VCC on 5 V. See §1.3.

**4. Watch the health counters.** With the link up, `SensorStatus` should show `frames_decoded`
climbing steadily even with nothing in front of the radar — a radar in an empty sky still emits
frames. If `bytes_discarded` is climbing and `frames_decoded` is not, the byte stream is not this
protocol: go back to step 1. The adapter reports this state as DEGRADED and names it.

**5. Turn on raw logging.** `raw_log.enabled` is already true in the shipped configuration.
Confirm the log is being written and that its session header carries the mount tilt.

**6. Run the test matrix below**, with raw logging on throughout.

**7. Only then build detection or alerting logic on this unit.**

---

## 5. Test matrix

Every session logs `mount_tilt_deg`. It is a first-class test variable, not a one-time setup
detail: one fixed tilt scans one fixed slice of sky, and a result recorded without it cannot be
compared with any other result. The raw log writes it into its session header automatically.

| Dimension | Values |
|---|---|
| Target | bare airframe; airframe with an RCS booster |
| Range | 10 m, 25 m, 50 m, 100 m |
| Angle | boresight, and both beam edges within ±20° |
| Motion | hover, approach, recede, lateral crossing |
| Mount tilt | 0° baseline for the full matrix first, then repeat the key cases at tilted angles |

Sweep `sensitivity` and `min_confirm_frames` across the matrix rather than fixing them. The
correct values against this target are exactly what the campaign is for.

The lateral-crossing case deserves specific attention: a target crossing the beam has a radial
velocity near zero regardless of how fast it is moving, so a nonzero `min_speed_kmh` will
suppress it entirely. That is a property of Doppler radar, not a fault.

### What the data is for

Three configuration blocks are placeholders until this campaign fills them in:

- `uncertainty.range_sigma_m` and `uncertainty.azimuth_sigma_deg` — currently conservative
  guesses, since the unit publishes no error model.
- `uncertainty.reported_confidence` — currently a flat number, honest about being one. Replacing
  it with something range- and cross-section-dependent requires measured detection statistics,
  which is what the matrix produces.
- `tuning.sensitivity` and `tuning.min_confirm_frames` — currently the most permissive settings,
  so that the first campaign sees everything the hardware is willing to report.

---

## 6. Explicit non-goals

- **No native elevation tracking.** Mechanical tilt only. It is not a sensor capability and is
  not simulated as one.
- **No multi-unit fusion.** Out of scope until more than one unit exists.
- **No raw signal processing.** The hardware exposes no IF data; there is nothing to process.
- **No production-grade detection confidence** until the matrix above has been run.

---

## 7. Running it

The driver needs `pyserial`, which is an optional dependency:

```
pip install -e ".[hardware]"
```

Nothing else in Aetos requires it, and the radar package imports without it — the import happens
inside `open()`, so the test suite and every simulation deployment run untouched.

Configuration lives in `config/radar/testbed_radar.json`. Site placement — where the unit is and
which way it faces — is separate and belongs in the site document, as it does for every sensor.
