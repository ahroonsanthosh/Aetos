# ADR 0019 — Altitude from sensors that do not measure it

**Status:** Accepted
**Date:** 2026-08-22
**Schema impact:** canonical Detection 1.0.0 → 1.1.0 (backwards-compatible addition)

## Context

`GeoPosition.altitude_m` is required and always has been. That was the right decision — Aetos is
an airspace system, altitude is load-bearing for zone floors and ceilings, and an optional
altitude would have every consumer branching on its absence.

Many real sensors do not measure it.

A two-dimensional ranging sensor reports range and azimuth and has no elevation channel at all.
An RF direction-finder reports bearing. Most fixed cameras report a bearing and, at best, a
range estimate from apparent size. None of these can say how high anything is, and the first
live driver written for Aetos is exactly such a device.

So the adapter must put a number in `altitude_m` for an object whose altitude it does not know.
Every available option is a statement, and the question is which statement, and how it is
recorded.

The hazard is specific and it is not the number itself. A track carries an altitude; the
operator interface displays it; the operator reads a figure rendered identically to one a
three-dimensional radar measured, and acts on it. **A field that always contains a number gives
no way to distinguish a measurement from an assumption**, and ADR 0011 exists precisely to stop
the interface presenting something the backend does not know.

A large vertical variance in `position_covariance_enu` states the uncertainty *numerically* and
is necessary, but it does not solve this: the covariance is not what the interface renders, and
"120 m" beside a σ of 15 m still reads as a measurement to a human under time pressure.

## Decision

### 1. The assumption is the sensor's own horizontal plane

An adapter with no elevation channel places the target at its own altitude — the target is
assumed to lie in the sensor's horizontal plane at the measured range.

This is the least-committal available assumption. It introduces no dependence on terrain data
the system does not have, it is exactly correct for the geometry a ground-based sensor is most
often looking at, and its error is bounded by the sensor's vertical beam extent rather than
being unbounded.

Ground level was considered and rejected: it requires a terrain model, and being wrong about
terrain produces the same silent hundred-metre error as being wrong about a datum (FM-004).

### 2. The vertical variance states the assumption numerically

`position_covariance_enu` carries a vertical variance derived from the sensor's vertical
aperture at the measured range:

```
σ_U  ≈  range × tan(vertical_aperture / 2) / √3
```

The √3 is the standard deviation of a uniform distribution over the beam's vertical extent,
which is the honest prior: within the beam, the adapter has no information favouring any
elevation over any other.

At 100 m with a ±15° vertical aperture this is roughly 15 m of one-sigma vertical uncertainty.
The filter therefore learns essentially nothing about altitude from this source, which is the
truth, and its own covariance says so — so a three-dimensional sensor observing the same object
will dominate the vertical estimate automatically, with no special-casing anywhere.

### 3. A per-detection marker records that the altitude was assumed

`GeoPosition` gains `altitude_source: AltitudeSource`, one of `measured`, `derived` or
`assumed`, defaulting to `measured`.

A dedicated enum rather than reusing `Provenance`. `Provenance` marks how an *inferred* value —
kinematics, classification — came to be, and its values are weighted by the classification
module (ADR 0012). Adding `assumed` to it would put a value into that weighting that means
nothing there, and would attach an altitude concept to two blocks that have no altitude.

Defaulting to `measured` makes this a backwards-compatible addition: every existing adapter,
and every detection already in the event log, keeps its current meaning without modification.
The schema goes to **1.1.0**.

### 4. The declaration is also made at the adapter level

`SensorCapabilities.reports_altitude` states the same fact statically, so it is visible before
anything is connected and can be checked by the conformance suite (ADR 0017). The per-detection
marker and the capability declaration must agree, and the suite fails an adapter where they do
not — an adapter declaring an altitude channel while emitting `assumed` on every detection is a
declaration error worth catching.

### 5. The interface renders an assumed altitude as not measured

Not as a number, not as a number with an asterisk, and not as a number in a lighter colour.
Where an assumed altitude appears in a position readout the interface shows that it is not
measured, with the sensor's assumption available on inspection.

This is the point of the whole record, and it is the part that would be quietly dropped first
under delivery pressure, so it is stated as a decision rather than left to the frontend.

## Rationale

The alternative — a large σ and no marker — is numerically honest and visually dishonest, and
this system is used by a human reading a screen. Aetos's stated position is that the interface
renders only what the backend actually knows (ADR 0011); an assumption presented in the same
typography as a measurement is a violation of that, and it happens to be a violation with an
altitude attached, which is the quantity most likely to be checked against a zone boundary.

Making the marker per-detection rather than only per-sensor matters for a case that will
certainly arise: a source that reports altitude when it can and assumes it when it cannot — a
camera with a rangefinder, a radar with an intermittent elevation channel. A per-sensor flag
alone would force such a source to declare the pessimistic answer permanently.

## Consequences

- Schema 1.1.0. Additive, defaulted, and readable by anything written against 1.0.0.
- The tracker is unaffected: it consumes position and covariance, and a large vertical variance
  is already handled correctly by the filter. No tracking change is needed, which is the
  strongest available evidence that the covariance mechanism was the right place to put the
  numerical half of this.
- Fusion, when it is built, gets the vertical resolution it needs for free — a 2-D source and a
  3-D source observing one object combine correctly by covariance weighting alone.
- The operator interface acquires a rendering rule it must apply everywhere altitude appears:
  the track list, the track details panel, the map data block, and the history page. Applying it
  in three of those four is worse than not having it, so it is enumerated here.
- A future terrain model would allow a better assumption than the sensor's own plane. That is a
  change to §1 only; the marker, the covariance treatment and the rendering rule stand
  regardless.

## Alternatives considered

- **Make `altitude_m` optional.** The obvious move, and it was reconsidered seriously. Rejected:
  every consumer would branch on absence, zone floor and ceiling logic would need a "no altitude"
  path, and the practical outcome is that a missing altitude gets defaulted to zero somewhere
  downstream by someone in a hurry — which is the same failure with no marker at all.
- **Large σ and no marker.** Rejected under Rationale: honest to the filter, dishonest to the
  human.
- **A sentinel value such as NaN.** Rejected: it propagates silently through arithmetic, and the
  first place it surfaces is a comparison against a zone floor that quietly returns false.
- **Assume ground level from a terrain model.** Rejected under §1 — no terrain model exists, and
  a wrong one reproduces FM-004 exactly.
- **Reject 2-D sensors below the interface floor entirely.** Rejected: it excludes most of the
  affordable sensor market for a limitation the system can carry honestly, and it contradicts
  ADR 0017.
