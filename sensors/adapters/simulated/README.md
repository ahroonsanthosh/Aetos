# `sensors/adapters/simulated` — the simulated sensor

## Responsibility

Turn ground truth into observations that are wrong in the ways real sensors are wrong.

This module is the practical half of [ADR 0002](../../../docs/adr/0002-detection-schema-provenance.md).
The simulator knows every object's exact position; the only thing standing between that and a
tracker quietly trained on perfect data is the configured degradation applied here. **Emitting
clean truth is a bug, not a shortcut.**

## The degradation chain

Between the truth source and an emitted `Detection`:

1. **Coverage** — range limits, azimuth sector, elevation bounds. Outside them, nothing.
2. **Probability of detection** — inside coverage is not the same as seen.
3. **Measurement noise** — anisotropic, drawn in the sensor's own along-/across-line-of-sight
   frame and resolved into ENU.
4. **Classification confusion** — the reported label is a draw, never a copy of the truth.
5. **False alarms** — Poisson-distributed returns generated from nothing at all.

## Two properties that are easy to get wrong

**Reported uncertainty must equal applied uncertainty.** `position_covariance_enu` is the
sensor-frame diagonal rotated by the azimuth — exactly the ellipse the noise was drawn from.
An adapter that understates uncertainty makes the downstream filter overconfident, which the
adapter contract names as worse than reporting nothing. The scalar
`position_uncertainty_m` is the *largest* of the three axes rather than their root-mean-square,
so a consumer reading only the scalar cannot end up believing the sensor is better than it is
in its worst direction.

**False alarms must be indistinguishable from real returns.** Same uncertainty, overlapping
confidence distribution, same optional blocks, and the emission order is shuffled so the
sequence of detection IDs carries no information either. Marking them, or giving them a
distinctive confidence, would let a downstream module separate real from spurious perfectly —
privileged information no real sensor supplies, which is ADR 0002's failure arriving through a
different door.

## Provenance

Everything optional this adapter emits is marked `simulated_truth`, including the noised radial
velocity. Range rate genuinely *is* measurable by a Doppler-capable sensor, and a future real
adapter would rightly mark it `measured` — but the number produced here originates in the
simulator's own state however much noise is added, and the provenance marker records where a
value came from rather than how plausible it looks.

Speed and heading are never emitted at all. They are quantities a tracker derives from a
sequence of positions, and no sensor this project models measures them.

## Determinism

Every draw comes from a generator derived from `(scenario seed, sensor_id)` — see `rng.py`.
One generator per sensor, so adding a fifth sensor to a scenario does not perturb the noise of
the four already there. The derivation uses a specified digest rather than Python's `hash()`,
which is salted per process and would make "deterministic" runs differ between invocations.
See [ADR 0008](../../../docs/adr/0008-determinism-backpressure-auth.md).

## Interface boundary

**Provides:** `SimulatedSensorAdapter` (a `SensorAdapter`), `SensorModel` and the registry
loader, `SensorDegradation`, `TruthObject` / `TruthSource`.

**Depends on:** `sensors.adapters.interface`, `sensors.schemas.detection`,
`backend.common.{clock, geodesy, identifiers}`, and `config/sensor_registry/` for its
parameters.

**Does not:** know what a scenario is. It reads ground truth through `TruthSource` and does
not know how trajectories are integrated, when objects appear, or that a scenario file exists.
`scenarios/world.py` supplies the implementation.

Placement — where a sensor sits and which way it faces — is not here either. That lives on the
site's `SensorPlacement`, because one model can be fitted at many sites; `registry_key` is the
join.

## Files

| File | Responsibility |
|---|---|
| `adapter.py` | The adapter: coverage, dropout, noise, confusion, false alarms |
| `sensor_model.py` | Versioned simulation parameters, as loaded from the registry |
| `registry.py` | Strict loading of `config/sensor_registry/` |
| `degradation.py` | Runtime performance loss a scenario can script |
| `truth.py` | `TruthObject` and the `TruthSource` seam |
| `rng.py` | Per-sensor seeded generators |
