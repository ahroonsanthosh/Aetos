# Running Aetos against real hardware

This is the practical guide to LIVE mode: one radar on a USB serial adapter, feeding the same
pipeline the simulator feeds.

**Read this first.** The radar this supports is testbed hardware and nothing it reports has
been validated. Its baud rate and frame layout are unconfirmed (FM-050), and whether it detects
a small airframe at any range has never been measured (FM-052). Live mode is honest about that
— it warns at start-up and the interface says so — but the system will happily draw a track
from a reflection off a passing van. Until you have run the test matrix in
`sensors/adapters/uart_fmcw_radar/README.md`, treat every track as something to check, not
something to believe.

Aetos remains software only. It ends at a recommendation to a human. There is no effector
control here and there will not be.

---

## 1. Wire it

```
LD2451 radar            CP2102 adapter          Host
VCC          <------>   5V
GND          <------>   GND
TX           <------>   RX
RX           <------>   TX
                        USB          <------>   USB port
```

**Set the CP2102's logic-level switch to 3.3 V.** The module runs on 5 V but its UART pins are
3.3 V logic; on 5 V you will over-drive them. Nothing in software can detect or prevent this.

Find the port:

- Linux: `ls /dev/ttyUSB*` — usually `/dev/ttyUSB0`. If you get a permission error,
  `sudo usermod -aG dialout $USER` and log out and back in.
- macOS: `ls /dev/tty.*` — usually `/dev/tty.SLAB_USBtoUART` or `/dev/tty.usbserial-*`.
- Windows: Device Manager → Ports (COM & LPT) — `COM3`, `COM4`, …

## 2. Prove the link before involving Aetos

```bash
.venv/bin/python -m sensors.adapters.uart_fmcw_radar.bringup --port /dev/ttyUSB0
```

It tries each candidate baud rate and reports which one produces **decoded frames** — not
which one produces bytes, because noise arrives at every rate and that is the whole problem.

```
Probing /dev/ttyUSB0 for a rate that frames:
   115200 baud … bytes arriving but nothing framed (2048 discarded)
   256000 baud … 37 frames decoded, 0 malformed, 12 bytes discarded

Frames decode at 256000 baud. Put that in the site's hardware block.
```

Then watch it, and walk in front of the sensor:

```bash
.venv/bin/python -m sensors.adapters.uart_fmcw_radar.bringup \
    --port /dev/ttyUSB0 --baud 256000 --record var/raw/session-01.log
```

Angles are relative to the sensor's own centreline, ranges in metres, speed signed with
negative meaning closing. If those numbers do not move sensibly when something moves, stop
here — nothing downstream can fix a link that is not working.

If nothing frames at any rate: check the 3.3 V switch, check TX↔RX are crossed and not
straight through, check 5 V and a common ground, and check nothing else already holds the port.

## 3. Describe your site

Copy `config/sites/hardware_testbed.json` and edit it, or edit it in place:

```json
{
  "site_id": "hardware_testbed",
  "reference": { "latitude": 51.8971, "longitude": -8.4697, "altitude_m": 5.0 },
  "protected_radius_m": 100.0,
  "sensors": [
    {
      "sensor_id": "radar_testbed_01",
      "position": { "latitude": 51.8971, "longitude": -8.4697 },
      "height_m": 1.5,
      "boresight_deg": 0.0,
      "enabled": true,
      "hardware": {
        "driver": "uart_fmcw_radar",
        "port": "/dev/ttyUSB0",
        "baud": 115200,
        "tilt_deg": 0.0,
        "min_range_m": 10.0,
        "max_range_m": 100.0,
        "raw_log_path": "raw/radar_testbed_01.log"
      }
    }
  ]
}
```

What each field actually costs you if it is wrong:

| Field | Get it wrong and |
|---|---|
| `reference` | the whole picture is somewhere else. It anchors the tracking frame and is immutable while running (ADR 0004) |
| `position` | every track is offset by your error, in the direction of your error |
| `height_m` | height above the **site reference altitude**, not above sea level. Added to the reference to give metres above the WGS-84 ellipsoid (FM-004) |
| `boresight_deg` | **the most common mistake.** Degrees clockwise from *true* north, not magnetic and not "roughly that way". Ten degrees out puts a target 17 m sideways at 100 m |
| `tilt_deg` | the sensor has no elevation channel, so this is the only elevation there is. Record it per session; do not assume it is still zero because it was yesterday (FM-051) |
| `baud` | nothing frames, and health reports DEGRADED naming the rate |
| `raw_log_path` | you lose the only record that can explain the session afterwards |

A placement is **either** simulated (`registry_key`) **or** real (`hardware`), never both. The
loader refuses anything else.

## 4. Run it

```bash
./start.sh --live                       # uses config/sites/hardware_testbed.json
./start.sh --live --site my_site        # your own site document
```

Then http://localhost:8000. The badge top-right reads **LIVE**, and the tooltip says the
hardware is unvalidated.

Under the hood:

```bash
.venv/bin/python -m backend.main --mode live --site hardware_testbed
```

### What each mode does, and what it refuses

| Mode | Sources | Refused when |
|---|---|---|
| `simulation` (default) | simulated adapters, driven by a scenario | a scenario is asked for at a site with no simulated sensors |
| `live` | this site's hardware only | the site has no enabled hardware placement, or a scenario was also asked for |
| `hybrid` | both at once | either half is missing |

The refusals are the point. Each one is a state where the display would have shown data that
was not what the badge above it claimed — a LIVE badge over an empty sky, or synthetic tracks
in a live picture with nothing saying so. `--mode live --scenario x` is refused by name so that
mixing the two has to be asked for deliberately.

## 5. Check it is actually seeing

**System Health** in the interface has a `sensors` row, read from the adapters themselves each
time you look:

- `OK` — the link is open and frames are decoding.
- `DEGRADED`, naming a baud rate — bytes are arriving and nothing is framing. Wrong rate, or
  the logic-level switch.
- `DEGRADED`, naming a silence — the link is open and the sensor has stopped talking.
- `OFFLINE` — the port could not be opened, or the device disappeared. Aetos retries on a
  backoff; unplugging and replugging the USB adapter recovers without a restart.
- `NOT_CONNECTED` — nothing is attached to this process at all.

One failed sensor degrades the row and names itself. It does not average out to healthy.

The start-up log reports the same thing before the browser opens, which is the cheapest moment
to notice a dead cable.

## 6. Interpreting what you see, honestly

- **Altitude is not measured.** Every track's height comes from `tilt_deg` and nothing else.
  The adapter reports a vertical uncertainty wide enough that the filter discounts it, but
  anything you read about height is a restatement of how you aimed the mount (FM-051).
- **Classification has no radar evidence.** This hardware emits no classification hint at all,
  so labels come from kinematics alone. Expect `unknown`.
- **Confidence is capped.** The signal-quality byte is uncalibrated, so it maps to a bounded
  confidence band rather than to a probability.
- **A vehicle-tuned radar sees vehicles.** Cars, vans and anything metal moving in the beam
  produce returns. At ground level with no tilt, most of what you see will be traffic.
- **Detections are stamped at receipt**, not at observation — later by the link latency, tens
  of milliseconds.

## 7. Before this goes anywhere but a bench

`./start.sh --live` binds loopback. Everything in **[docs/deployment.md](deployment.md)**
applies before it binds anything else: create an account, put TLS in front, pass
`--behind-proxy`. The startup guard refuses the unsafe combinations rather than warning about
them.

And the standing caveat: the detection performance of this hardware against a small airframe is
unmeasured. Live mode makes the system *usable*; it does not make the sensor *trustworthy*.
That takes the test matrix, and the test matrix takes flying something in front of it and
writing down what happened.

## References

- `sensors/adapters/uart_fmcw_radar/README.md` — the hardware, the wiring, the bring-up order
- ADR 0017 — why a real sensor is an adapter and not a new interface
- ADR 0018 — why run mode selects the adapter set, and why each refusal exists
- `docs/failure_modes.md` — FM-050, FM-051, FM-052
