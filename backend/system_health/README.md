# System health

**Noticing that the picture has thinned, and saying so.**

## The problem

A sensor stops producing. Its tracks coast, go STALE, then LOST — and throughout, the interface
keeps drawing confident-looking positions derived from data that no longer exists. Nothing
anywhere says the picture has thinned.

That is FM-010, and it matters far more on a site than on a laptop, because on a laptop somebody
is watching the terminal.

## The ambiguity at the centre of it

**A sensor producing nothing may be broken, or may be looking at an empty sky.** One sensor,
watched alone, cannot distinguish those, and guessing is wrong in one of two expensive
directions:

- Guess **broken** and it cries wolf through every quiet minute until an operator learns to
  ignore the indicator — the failure ADR 0015 spends its whole length avoiding for alerts.
- Guess **empty sky** and it stays silent through exactly the event it was built to catch.

So it does not guess. It uses the one piece of evidence a single sensor does not have: **the
other sensors.** Silence while neighbours are producing steadily is anomalous. Silence while
nothing anywhere is producing is a quiet sky, reported as OK with the reason saying so in as
many words.

The rule degrades honestly. A single-sensor site has no neighbours, so it can never call its
sensor silent — and the reason text says that, rather than implying a health check that is not
happening. That is the configuration the testbed radar will run in.

## Why thresholds are configured and not derived

The first version derived the threshold from each sensor's update rate: eight missed update
periods and the sensor was silent. That is wrong, and measurably so.

**Update rate is how often the adapter polls. It is not how often a target is in coverage.** A
10 Hz narrow-field EO/IR sensor polls every 100 ms and sees the target only while it is in its
field of view. Measured across all nine shipped scenarios, with no fault present anywhere:

| Sensor | Longest legitimate quiet spell |
|---|---|
| `eo_ir_east` (narrow field, 10 Hz) | **81 s** |
| `radar_north` (wide, 4 Hz) | 10.5 s |
| `rf_central` (wide, 1 Hz) | 8 s |

Against a derived threshold of 5 s, that flagged the healthiest sensor on the site continuously.
There is no single number right for every sensor, because quiet time is dominated by coverage
rather than by health — so the threshold is declared, per FM-010's own wording (*"silence past a
configured threshold"*), resolved most-specific-first: sensor id, then sensor type, then the
site default. A sensor's poll rate is reported alongside its health as context, never used as a
substitute for knowing it.

## Conditions

| Condition | Meaning |
|---|---|
| `not_started` | Constructed but never started. Nothing has gone wrong yet. |
| `ok` | Running and producing as expected. |
| `degraded` | The adapter reports reduced capability it can name. Still contributing. |
| `silent` | Running, link up, producing nothing past its allowance — **while others produce**. |
| `offline` | Stopped or unable to observe. The adapter says so itself. |

A sensor that has never produced anything is never called silent: nothing yet in its coverage is
indistinguishable from blind, and calling it silent would flag every short-range sensor at a site
whose action is far away. The count of zero is on the record for an operator to read; the
conclusion is not drawn for them.

## Degraded mode

When contributing sensors fall below `minimum_contributing_sensors`, the site is degraded and
`GET /system/health` reports it as `degraded` rather than OK-with-a-note — the whole point is
that a thinned picture is visible without reading the detail. `DEGRADED_MODE_ENTERED` and
`DEGRADED_MODE_EXITED` are recorded on transition.

A site with **no** sensors configured is worded differently from one whose sensors have failed:
nothing was ever attached, so there is nothing to have gone wrong, and "0 of 0 sensors are
contributing" would read as something broken.

## What it must not do

**It must not suppress alerts** (`docs/architecture.md`, module map). A degraded site is a reason
to tell an operator more, never less. Nothing here touches alerting, and a track's risk is scored
the same whether the watchdog is happy or not — a system that quietened down as its sensing
failed would be at its most reassuring when it was least able to see.

## Scenario support

`e_sensor_degradation` scripts a radar degrading at 100 s and going offline at 200 s. Those
windows were dead configuration until this module landed: `apply_degradation` existed on the
simulated adapter and **nothing called it**, and the scenario named a sensor id no site defines,
so the one scenario written to exercise sensor failure exercised nothing and said nothing. The
pipeline now applies availability windows every cycle and refuses at attach time to run a
scenario that scripts a sensor the site does not have.

## Files

| File | Purpose |
|---|---|
| `models.py` | `SensorCondition`, `SensorObservation`, `SystemHealth` |
| `policy.py` | Silence thresholds and the degraded-mode floor |
| `watchdog.py` | Polling, the corroborated-silence rule, recording |

Configuration is `config/system_health/policy.json`. See FM-010 and
[ADR 0018](../../docs/adr/0018-sensor-watchdog.md).
