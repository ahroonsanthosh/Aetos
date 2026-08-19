# ADR 0018 — Sensor silence is judged against the other sensors, not against a clock

**Status:** Accepted
**Date:** 2026-08-19
**Closes:** [FM-010](../failure_modes.md)

## Context

A sensor stops producing. Its tracks coast, go STALE, then LOST, and throughout that the
interface keeps drawing confident-looking positions from data that no longer exists. Nothing
says the picture has thinned.

`SensorAdapter.health()` already reported the facts a watchdog needs — state, last detection
time, count, and a reason in words — and deliberately declined to interpret them, because the
threshold that is right for a 10 Hz camera is wrong for a 1 Hz radio-frequency sensor
(`sensors/adapters/README.md`). Nothing consumed those records.

## The difficulty

**A sensor producing nothing may be broken, or may be looking at an empty sky.** One sensor,
watched alone, cannot distinguish those, and both guesses are expensive:

- Guess **broken**, and the indicator fires through every quiet minute until an operator learns
  to ignore it — the desensitisation ADR 0015 spends its length avoiding for alerts, arriving
  through a different door.
- Guess **empty sky**, and it stays silent through exactly the event it exists to catch.

## Decision

**Silence is anomalous only when other sensors are producing.**

That is not a heuristic standing in for something better; it is the actual discriminating
information available. A sensor alone in a quiet sky is indistinguishable from a failed one. A
sensor producing nothing while its neighbours produce steadily is anomalous in a way one sensor's
own silence never is.

Two conditions narrow it further, and both came from measurement:

- **The sensor must have produced something before.** A sensor with nothing yet in its coverage
  is indistinguishable from a blind one, and calling it silent would flag every short-range
  sensor at a site whose action is far away.
- **Thresholds are declared, not derived from update rate.** See below.

The rule degrades honestly. A single-sensor site has no neighbours, so it can never call its
sensor silent, and the reason text says so rather than implying a check that is not happening.
That is the configuration the testbed radar will run in.

### Thresholds are configuration

The first implementation derived the threshold from each sensor's update rate — eight missed
update periods and the sensor was silent. That is wrong, and measurably so. **Update rate is how
often the adapter polls; it is not how often a target is in coverage.**

Measured across all nine shipped scenarios with no fault present anywhere:

| Sensor | Longest legitimate quiet spell | Derived threshold |
|---|---|---|
| `eo_ir_east` (narrow field, 10 Hz) | **81 s** | 5 s |
| `radar_north` (wide, 4 Hz) | 10.5 s | 5 s |
| `rf_central` (wide, 1 Hz) | 8 s | 8 s |

The healthiest sensor on the site was flagged continuously. No single number is right for every
sensor because quiet time is dominated by coverage rather than health, so the threshold is
declared — per FM-010's own wording, *"silence past a configured threshold"* — resolved
most-specific-first: sensor id, then sensor type, then site default. Poll rate is still reported
alongside each sensor's health, as context, never as a substitute for knowing.

### Degraded mode

Below `minimum_contributing_sensors`, the site is degraded and `GET /system/health` reports
`degraded` rather than OK-with-a-note. A thinned picture has to be visible without reading the
detail.

### What it must not do

**It must not suppress alerts**, per the module map's "must not" column. A degraded site is a
reason to tell an operator more, never less. Nothing here touches alerting, and a track's risk is
scored the same whether the watchdog is happy or not — a system that quietened down as its
sensing failed would be at its most reassuring when it was least able to see.

## Consequences

The watchdog is given each sensor's expected interval by whatever assembled the adapters, rather
than reading the sensor registry itself. Reading the registry would make it work only for sensors
that have registry entries, which real hardware may not.

Scripted sensor availability now actually happens. `apply_degradation` existed on the simulated
adapter and **nothing called it**, and `e_sensor_degradation` named a sensor id no site defines —
so the one scenario written to exercise sensor failure exercised nothing, and said nothing about
it. Two further scenarios had the same dangling references. The pipeline applies availability
windows every cycle, recomputed from elapsed scenario time so a window cannot be missed between
cycles and a replay is identical (ADR 0008), and refuses at attach time to run a scenario
scripting a sensor the site does not have.

FM-010's stated handling is now testable rather than assumed: the scripted radar degrades at
100.0 s and goes offline at 200.0 s, and the watchdog records both transitions with the
scenario's own operator-readable reasons.

## What this does not fix

A sensor whose link is healthy and whose hardware is blind, at a site where nothing else covers
its volume, is still undetectable — there is no evidence anywhere in the system that would
separate it from an empty sky. The honest response is the one taken: report the facts, including
a detection count of zero, and decline to draw the conclusion for the operator.
