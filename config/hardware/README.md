# `config/hardware/` — sensors that physically exist

One document per fitted device, named by the `registry_key` it declares. A site placement in
`config/sites/` names a key; if the key resolves here rather than in `config/sensor_registry/`,
that placement is a real sensor on a real port rather than a simulation.

That is the whole of the simulated-versus-real decision, and it is why these are two
directories and not one file with a type field. A merged directory lets a typo fall back from a
device to a simulation profile, and an operator watching synthetic returns while believing they
are measurements is the worst state this system can reach.

## What a document holds

| Block | Holds | Changes when |
|---|---|---|
| `link` | Serial port, baud rate, read timeout, silence timeout | The device is plugged into a different machine |
| `protocol` | The two wire sign conventions | Never, once determined on the bench |
| `coverage` | Range gate, azimuth extent, elevation beamwidth | The device is re-tuned, or measured properly |
| `uncertainty` | One-sigma range, bearing and speed error | Somebody measures the device |
| `quality` | How its signal level maps to a confidence in [0, 1] | Somebody measures the device |
| `tilt_deg` | Elevation of boresight above horizontal | The housing is physically re-aimed |

## These numbers are not a datasheet

The shipped figures are pessimistic placeholders. The vendor publishes a rated range for
vehicle-scale targets and nothing about bearing accuracy, range error or elevation beamwidth,
and nothing at all about performance against a small airframe.

Understating uncertainty makes the tracking filter overconfident, which is worse than reporting
none — it is the adapter obligation in `docs/detection_schema.md` that this directory exists to
satisfy. Measure the device, then write down what you measured:
`docs/hardware_radar_bench.md` §7.

## `silence_timeout_s` is not a detection timeout

The device frames a report on its own cadence whether or not anything is in front of it. A link
carrying empty reports is a sensor watching an empty sky; a link carrying nothing at all is a
sensor that has stopped talking to us. Only the second is a fault, and `silence_timeout_s`
bounds only the second (FM-010).
