# `sensors/config` — deployment configuration for physical sensors

## Responsibility

Where the streams are, what the thresholds are, what the sensors are called, and where they
are mounted. Facts about *this deployment*, which must not require a code change to alter.

Not the same thing as `config/sensor_registry/`, which describes what a *kind* of sensor can
physically do and is what the simulator degrades ground truth through. A registry entry
describes a class of sensor; this file describes the two units on the bench.

## Contents

| File | |
|---|---|
| `sensor_config.yaml` | The document. Commented per field. |
| `loader.py` | Strict, validating loader. |

## Strict loading

An unknown key is an error, not an ignored line, and a value outside its bounds fails at load
rather than at first use. A tolerant loader's failure mode is a site running with a threshold
nobody set, which is indistinguishable from a site running with the threshold somebody meant
to set.

`schema_version` is checked against the version the build reads, so a configuration written
for a different shape of the document is rejected instead of half-applied.

## Who reads this

Two callers, and they resolve the site differently:

- **`sensors/live.py`**, when the backend runs with `--live`. It is handed the backend's own
  ENU frame, anchored at the site reference point in `config/sites/`, and `site_origin` below
  is ignored. The site file is the single authority on where the site is.
- **`sensors/tests/manual_camera_test.py`**, the standalone bench script, which runs without a
  site loaded and so falls back to `site_origin` here.

`position_enu` is a sensor's offset within whichever frame applies, which is the same number
either way.
