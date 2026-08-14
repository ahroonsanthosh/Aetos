# `config/radar/` — one document per physical radar unit

## Format

One JSON document per unit, named by its `sensor_id`, validated against `RadarConfig` in
`sensors/radar/config.py`. The declared `sensor_id` must match the file name, for the reason
every other configuration document carries the same check: a file copied and edited but not
renamed puts one radar's mount tilt on another radar's returns.

| Block | Holds |
|---|---|
| `link` | Serial port, baud rate, read timeout, reconnect backoff |
| `tuning` | Settings pushed into the unit's own firmware: distance, minimum speed, sensitivity, confirmation frames |
| `mount` | Bearing, tilt, height, and the beam's azimuth and elevation extent |
| `uncertainty` | One-sigma measurement error, and the confidence stamped on every detection |
| `raw_log` | Whether and where to log every frame |

`model` is free text carried into the raw log and diagnostics. It is data, never a branch — no
code reads it to decide what to do, which is what keeps a hardware swap a configuration and
driver change rather than a code change.

## Placement is not here

Where the unit sits on the site — latitude, longitude, and which entry in the sensor picture it
corresponds to — belongs in the site document, as it does for every sensor. This file describes
the device; the site describes where devices are. `mount.boresight_deg` and `mount.height_m` are
here rather than there because they are inseparable from the beam geometry the driver's layer
resolves, and because tilt has no equivalent in the site schema at all.

## Three fields that are placeholders, and say so

The shipped `testbed_radar.json` describes hardware that has not been characterised against the
intended target. Three values in it are conservative guesses awaiting the campaign in
`docs/radar_testbed.md`:

- `link.baud_rate` — **unverified.** Sources disagree between 115200 and 256000.
  `probe_on_connect` is on so that the first connection reports which one actually frames. Write
  that answer in and turn probing off.
- `uncertainty.range_sigma_m` / `azimuth_sigma_deg` — the unit publishes no error model. These
  are deliberately loose: understating uncertainty makes the tracker overconfident, which is
  worse than reporting nothing.
- `uncertainty.reported_confidence` — a flat number, honest about being flat. There are no
  measured detection statistics for a small airframe against this unit, so there is nothing to
  compute a per-detection confidence from.

## Mount tilt

`mount.tilt_deg` is the only way this unit achieves any elevation coverage, and it is a first-class
test variable rather than a setup detail. It rotates the entire beam: upward coverage is bought by
giving up ground-level coverage, and one fixed tilt scans one fixed slice of sky. The driver writes
it into the header of every raw log session, because a set of returns recorded without the tilt
that produced them cannot be compared with any other set.

## Test hardware only

The shipped document describes a testbed unit on a bench. It is not a production deployment, and
nothing in it should be read as a specification for, or a claim about, any sensor Aetos will ship
with. See ADR 0017.
