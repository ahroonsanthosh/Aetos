# ADR 0004 — Tracking maths runs in a local ENU tangent plane

- **Status:** Accepted
- **Date:** 2026-08-08

## Context

The canonical Detection carries WGS-84 latitude, longitude and altitude. This is correct for
an interchange format: it is unambiguous, universally understood, and does not depend on any
particular site.

It is wrong for tracking maths. Degrees are not metres, and a degree of longitude is not a
fixed distance — it shrinks with the cosine of latitude. Running a Kalman filter or an
association gate directly on degrees produces uncertainty ellipses that are wrong, and wrong
by a different factor in each axis. Track crossing (scenario H) is precisely the situation
where that error causes an identity swap.

## Decision

- The canonical schema stays WGS-84 lat/lon/alt. Unchanged.
- At the **ingestion boundary**, every detection is converted to a local **ENU**
  (East, North, Up) Cartesian frame in metres, anchored at the configured site reference
  point.
- All tracking, association, gating, prediction, fusion, zone geometry and risk geometry
  operate in ENU metres.
- At the **API boundary**, track positions are converted back to WGS-84 for output.

Conversion is WGS-84 geodetic → ECEF → ENU, implemented in `backend/common/geodesy.py` with
no external geodetic dependency.

## Rationale

This confines the coordinate conversion to two well-defined boundaries rather than
scattering `cos(latitude)` corrections through the maths. Every module between those
boundaries works in a flat metric frame where Euclidean distance means what it says.

The tangent-plane approximation introduces error that grows with distance from the anchor.
At C-UAS ranges — a protected radius on the order of a kilometre, sensor ranges in the
low tens of kilometres — that error is well under a metre and far below sensor measurement
uncertainty. The approximation is documented and its validity range is asserted in tests.

## Consequences

- Sites are bounded in extent. A site spanning hundreds of kilometres would need a different
  projection strategy. This is recorded as a known limitation in `docs/failure_modes.md`.
- The site reference point is load-bearing configuration, not decoration. Changing it
  invalidates in-flight track state, so it is treated as immutable for the lifetime of a
  running system.
- Altitude is metres above the WGS-84 ellipsoid throughout. Not above ground level, not
  above mean sea level. Stated explicitly in `docs/detection_schema.md` because this is a
  classic source of silent hundred-metre errors between systems.
