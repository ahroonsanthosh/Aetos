# `sensors/schemas` — the canonical Detection

## Responsibility

One file, one job: `Detection`, the shape every source of observations must produce.

This is the narrow waist of the entire system. The simulator and every real sensor adapter emit
exactly this, and nothing downstream of ingestion can tell which produced a given detection —
which is the governing principle of the architecture, not a nicety.

## Why it lives under `sensors/` and not `backend/`

Because adapters must be able to depend on it without depending on the backend. `sensors/`
imports `backend.common` for the clock and nothing else; putting the schema in `backend/` would
invert that and make the sensor layer a client of the application it feeds.

## What the schema insists on

- **WGS-84, metres above the ellipsoid, SI throughout.** Converting is the adapter's job, and
  getting it wrong is a silent hundred-metre error (FM-004).
- **Timezone-aware timestamps**, of when the observation was *made*. Naive datetimes are rejected
  at the boundary because an ambiguous timestamp produces velocity estimates wrong by the
  timezone offset (FM-012).
- **Honest uncertainty.** `position_uncertainty_m` is required; `position_covariance_enu` is
  optional and preferred, because real sensor error is anisotropic and a scalar cannot say so.
  Ingestion rejects uncertainty a filter cannot use (FM-011).
- **Provenance on anything derived.** `kinematics` and `classification` are optional and marked
  with how they came to hold their value, so "the sensor measured this" and "the simulator knows
  this because it created the object" are distinguishable in the data (ADR 0002).

## Changing it

`DETECTION_SCHEMA_VERSION` is carried on every detection. Bump the minor for a
backwards-compatible addition, the major for anything that changes the meaning of an existing
field — and remember the event log holds detections written under older versions, which replay
must still be able to read.

See `docs/detection_schema.md` for the field-by-field contract.
