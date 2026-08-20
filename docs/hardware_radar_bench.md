# Bench integration: the serial radar

How to get a physically fitted radar producing tracks in Aetos, what to verify before
believing anything it shows you, and what this device cannot do regardless of configuration.

This is a **bench-test integration**. One short-range module, one serial link, one site. It is
enough to prove the source boundary end to end against real hardware; it is not a sensor
suite, and nothing here should be read as a claim that Aetos is fitted for a site.

---

## 1. The hardware

**Radar module** — 24 GHz FMCW, vehicle-detection firmware, ±20° azimuth, rated to 100 m,
5 V supply at ~107 mA, 3.3 V TTL UART.

**USB-to-TTL adapter** — CP2102, with a selectable 3.3 V / 5 V logic jumper.

Two things about the power arrangement matter and are easy to get wrong:

- The **logic level must be 3.3 V**. The radar's UART pins are 3.3 V; driving them at 5 V is
  out of spec for the part.
- The radar must be powered from the adapter's **5 V pin**, not its 3.3 V pin. The 3.3 V rail
  on these adapters is rated around 40 mA, and the radar draws roughly 107 mA average with a
  higher peak. Powering it from the 3.3 V pin produces a module that brownouts under load —
  which shows up as a link that works until something enters the beam.

### Wiring

| Adapter | Radar | Note |
|---|---|---|
| 5 V | VCC | Supply. Not the 3.3 V pin — see above. |
| GND | GND | Shared ground. A floating ground is the usual cause of a link that decodes garbage. |
| TX | RX | Adapter transmit to radar receive |
| RX | TX | Adapter receive to radar transmit. **Crossed.** |

Jumper on **3.3 V**.

---

## 2. Find the port

```bash
ls /dev/serial/by-id/          # Linux — the stable name; prefer it over /dev/ttyUSB0
ls /dev/cu.usbserial-*         # macOS
```

On Windows, Device Manager under *Ports (COM & LPT)*; the port is `COM3` or similar.

On Linux you may need to be in the `dialout` group to open the port:

```bash
sudo usermod -a -G dialout "$USER"     # log out and back in for it to take effect
```

---

## 3. Prove the link before involving the pipeline

```bash
.venv/bin/python -m scripts.radar_probe --port /dev/ttyUSB0
```

This opens the port, decodes frames and prints them. Nothing else — no database, no event log,
no tracks. It is the smallest thing that distinguishes a wiring fault from a sensor fault, and
it is worth running whenever the operator picture later looks wrong.

Expected: a stream of empty reports when nothing is in front of the sensor, and lines with a
range, azimuth and speed when something is. **Empty reports are the healthy state** — the
device frames on its own cadence whether or not it sees anything, and that is exactly what
lets the adapter tell a working sensor watching an empty sky from a link that has died.

If nothing decodes at all, in order of likelihood: TX/RX not crossed, the jumper on 5 V, no
5 V supply to the module, or a baud rate other than 115200. Add `--raw` to see the bytes
before decoding.

---

## 4. Determine the two sign conventions

Both are configuration in `config/hardware/bench_radar_uart.json`, and both are wrong in ways
that look plausible on a map. Determine them; do not assume them.

**Direction code.** With the probe running, walk **towards** the sensor.

- Lines print `approaching` → `"approaching_code": 1` is correct.
- Lines print `receding` → set `"approaching_code": 0`.

Getting this backwards inverts the sign of every range rate the system records: a target
closing on the protected area is logged as leaving it.

**Azimuth sign.** Stand clearly **to the right** of boresight (looking out along it).

- Azimuth prints positive → `"azimuth_sign": 1` is correct.
- Azimuth prints negative → set `"azimuth_sign": -1`.

Getting this backwards mirrors the picture about the boresight axis. On a symmetric site,
nothing about the display looks wrong.

---

## 5. Configure the site and the mount

Two documents describe the installation:

**`config/hardware/bench_radar_uart.json`** — the device: serial port, the two sign
conventions above, coverage gate, error figures, and the **mount tilt**.

**`config/sites/bench_radar_test.json`** — where it is: the site reference point, the sensor's
own position, its height, and its boresight bearing. Zone radii here are tens of metres, sized
for a 100 m sensor rather than the kilometre-scale zones of the simulation site.

Both ship with the fictional test range's coordinates as placeholders. **Replace them with
where the sensor actually is** before reading anything into the map: the reference point
anchors the local frame, and a site anchored somewhere else puts every detection somewhere
else with complete confidence.

`boresight_deg` is degrees clockwise from **true** north, not magnetic. A phone compass gives
magnetic; the difference is several degrees in most of the world and it is a systematic
bearing error on every detection.

### Tilt, and the elevation the device does not measure

The device reports **no elevation**. Its beam covers about ±20° in azimuth and an unspecified
extent in elevation, fixed relative to the board. The only way to cover airspace above the
horizon is to physically tilt the housing — and if you do, `tilt_deg` in the hardware document
must say by how much.

The adapter places every detection at the centre of the beam at that tilt, and gives it a
vertical uncertainty of `range × beamwidth / √12` — 10 m at 100 m with a 20° beam. That is
not a measurement error; it is the honest width of "somewhere in the beam". A tilt in the
housing that is not in the file puts every detection off by `range × sin(error)`: 17 m at
100 m and 10°.

---

## 6. Run it

```bash
./start.sh --live                                  # uses the port in the hardware document
./start.sh --live --radar-port /dev/ttyUSB1        # override it
```

`--live` installs the serial driver (an optional extra), defaults the site to
`bench_radar_test`, and refuses to run alongside `--scenario` — one display carrying real and
synthetic returns with nothing marking which is which is precisely the confusion the mode
indicator exists to prevent.

`GET /api/v1/system/mode` then reports `mode: live` with `real_adapters_connected: 1` and
`simulated: false`. If it reports live with zero adapters, the configuration and the hardware
disagree, and the interface will keep saying so rather than resolving it in favour of the more
reassuring answer.

A port that will not open **stops startup**. That is deliberate: a system that started anyway
would show an operator exactly what a working sensor over an empty field shows.

---

## 7. Measure it, then write down what you measured

The figures shipped in `config/hardware/bench_radar_uart.json` are pessimistic placeholders,
not a datasheet. The vendor publishes a rated range for vehicle-sized targets and nothing about
bearing accuracy, range error or elevation beamwidth. Understating uncertainty makes the
tracking filter overconfident, which is worse than reporting none.

Worth measuring, in rough order of value:

| Figure | How | Where it goes |
|---|---|---|
| Detection range on your actual target | Walk or fly it out until reports stop | `coverage.max_range_m` |
| Range accuracy | Stand at a tape-measured distance; compare reported range | `uncertainty.range_sigma_m` |
| Bearing accuracy | Stand at a known angle off boresight; compare reported azimuth | `uncertainty.azimuth_sigma_deg` |
| Azimuth extent | Walk across the beam; note where reports start and stop | `coverage.azimuth_extent_deg` |
| Signal level on a real target | Read the probe's `signal` column | `quality.signal_level_full_confidence` |

**The single most important measurement is the first.** The rated 100 m is for a vehicle-scale
radar cross-section. A small airframe presents a far smaller one, and the range at which this
device sees one is unknown until you have walked it out. Nothing in the software compensates
for that, and nothing should: the coverage numbers are a gate on what is believed, not a
promise about what will be seen.

The vendor's own Bluetooth configuration app adjusts the module's internal detection distance
and sensitivity. Aetos never writes configuration to the device while running — a detection
range that changed underneath a live track would be a change to what the sensor is, with no
record of it in the operator's picture.

---

## 8. What this integration does not give you

- **No elevation**, so no honest altitude. Everything is at beam centre with the uncertainty
  to match. Zone floors and ceilings are effectively untestable with this sensor alone.
- **No classification.** The device detects motion; it does not identify what is moving. Every
  track from it classifies on kinematics and geometry alone.
- **One sensor**, so no fusion, no cross-sensor association, and no way for a second source to
  contradict a false alarm.
- **Vehicle-tuned firmware**, unverified against small airframes.

The honest summary is that this proves the source boundary works against real hardware, and
gives a real short-range picture of ground-level movement. Read anything more into it and the
system is being asked to claim something it cannot.
