# ADR 0017 — The first real sensor adapter

**Status:** Accepted
**Date:** 2026-08-20
**Supersedes:** nothing. Closes the "no real sensor integration exists" statement that
`docs/architecture.md`, `docs/detection_schema.md` and `sensors/adapters/README.md` all
carried.

## Context

Every architectural claim in this repository rests on one sentence: *the tracking engine
cannot tell whether a detection came from the simulator or from a physical sensor*. Until now
that sentence was untested in the only way that counts. The simulated adapter was written
against the same interface, but it was written by the same people at the same time as the
interface, and an abstraction validated solely by its first and only implementation is an
abstraction that has not been validated.

A short-range 24 GHz FMCW radar module with a 3.3 V serial interface became available. It is a
modest device — vehicle-detection firmware, ±20° azimuth, rated to 100 m against vehicle-scale
returns, and **no elevation output whatsoever**. It is not a sensor anyone would fit to a site.
It is, precisely because of its limitations, a good test of whether the source boundary holds:
it violates several assumptions the simulated adapters quietly satisfy.

## Decision

Integrate it as `sensors/adapters/radar/`, changing nothing below ingestion.

### 1. The adapter is an addition, not a modification

No module in `backend/tracking`, `backend/fusion`, `backend/classification`, `backend/risk` or
`backend/decision` changed. The pipeline's step function is unchanged and does not know which
kind of adapter it is draining. This was the prediction the architecture made, and it held.

What *did* change, and had to:

- `ApiServices.real_adapter_count()` stopped returning a structural zero and started reading
  the pipeline's attached adapters. It was written as a computed zero for exactly this day.
- `backend/main.py` gained `--live`, and its pipeline task now runs whenever anything is
  attached rather than only when a scenario is.
- `ConfigPaths` gained `config/hardware/`.

### 2. Fitted hardware is a separate configuration directory from simulation profiles

`config/sensor_registry/` holds simulation profiles — how to degrade ground truth.
`config/hardware/` holds fitted devices — which port, which mounting, how much to believe them.
Both are keyed by `registry_key`, and which directory a site placement's key resolves in is the
entire simulated-versus-real decision.

The alternative — one directory with a type discriminator — was rejected because a typo in it
falls back from a real sensor to a simulated profile, and an operator watching synthetic
returns believing they are measurements is the worst state this system can reach.

### 3. The unmeasured vertical is admitted in the covariance, not assumed away

The device reports no elevation. Three options existed: refuse to emit a position at all;
assume a plausible altitude; or place the detection at beam centre with an uncertainty that
describes the beam.

The first makes the device useless. The second is FM-004 in its purest form — a number
indistinguishable from a measurement that is an assumption. The third is what the canonical
schema's `position_covariance_enu` exists for: the detection is placed at the configured mount
tilt with a vertical sigma of `range × beamwidth / √12`, the standard deviation of a uniform
distribution across the beam. At 100 m through a 20° beam that is 10 m, against a range sigma
of 2 m. The filter is told, in the only channel it reads, that this sensor knows where
something is horizontally and very nearly nothing about its height.

### 4. The adapter asserts no classification

The firmware is tuned for vehicles, and emitting `ClassificationLabel.VEHICLE` would have been
easy and defensible-sounding. It was rejected: a `ClassificationHint` is evidence the
classification module weights, and this device determines nothing about what a reflector is.
Silence is the truthful output, and it is what the adapter emits.

By the same reasoning the device's unitless signal level is **not** written to
`Quality.snr_db`. That field is named in decibels; the level is not decibels.

### 5. Two wire conventions are configuration, and stay configuration

Which direction code means *approaching*, and which way the azimuth sign runs, are not settled
by the device's published documentation, and independent implementations written against it
disagree. Guessing produces a system that inverts every range rate, or mirrors the picture
about the boresight axis — both of which look entirely plausible on a display.

They are therefore fields in the hardware document with a thirty-second bench procedure for
determining each (`docs/hardware_radar_bench.md` §4), and tests that fail if either is quietly
turned back into a constant.

### 6. `--live` and `--scenario` are refused together

HYBRID mode remains unbuilt. Allowing the flags to combine would have produced it by accident,
with real and synthetic tracks sharing one display and nothing on screen distinguishing them.
The startup refusal is the honest form of "not built".

### 7. The adapter reads a transport, so it is testable without the device

`ByteTransport` has two implementations: a serial port and a replay of captured bytes. Every
test — framing, resynchronisation, geometry, uncertainty, health, and a full pipeline run that
ends in a track — runs on a machine with no serial port. A hardware integration testable only
with the hardware attached is one that stops being tested the first week nobody has it plugged
in.

## Consequences

**LIVE mode is now partially built, and the partiality must be stated wherever it is claimed.**
One short-range sensor, no elevation, no classification, no fusion, and no verified performance
against a small airframe — the rated range is for vehicle-scale radar cross-sections, and what
this device does with a small UAS is unknown until someone measures it. Every document that
touches this says so, and `docs/hardware_radar_bench.md` §7 is how the measuring is done.

**The narrow waist held.** That is the finding worth recording: the second implementation of
`SensorAdapter`, written against real hardware with genuinely awkward properties, required no
change below ingestion.

**The vendor stays out of the source.** No manufacturer, model or product name appears in
`backend/` or `sensors/`. The device is described by what it does — a UART target-report radar
— and named only in configuration and in the bench document, which is where an operator needs
to read it.
