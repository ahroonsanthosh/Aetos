# `sensors/adapters/hardware_radar` — a physical radar unit as a canonical source

## Responsibility

The point at which hardware stops being hardware. Above it, Aetos sees `Detection` objects and
cannot tell whether they came from a radar on a tripod or from the simulator. Below it, a
`RadarSensor` (`sensors/radar/`) reports ranges and angles in its own frame and knows nothing
about the world.

## Interface boundary

**Provides:** `HardwareRadarAdapter`, a `SensorAdapter`.

**Depends on:** the `RadarSensor` interface — never a concrete driver — plus
`backend.common.geodesy` for the site frame, `backend.common.clock` for time, and
`backend.common.identifiers` for detection IDs.

**Does not:** import any driver module, read `TargetDetection.raw_source` to decide anything, or
know which model of radar it is holding. `tests/unit/test_module_boundaries.py` fails the build if
that stops being true (ADR 0017).

## What it does with a return

1. **Resolves the beam.** Range and off-boresight angle, plus the mount's bearing and tilt,
   become an ENU offset from the sensor and a 3x3 covariance (`sensors/radar/geometry.py`).
2. **Converts to WGS-84** through the site's local frame, altitude as metres above the ellipsoid.
3. **Timestamps the observation**, not the poll. Each target carries the monotonic time its frame
   decoded; the elapsed interval since then is subtracted from wall time, so a poll that drains a
   cycle's worth of frames does not compress them onto one instant.
4. **Marks provenance.** Radial velocity is a genuine Doppler measurement, marked `measured`.
   Speed and heading are left absent — a radar does not measure them (ADR 0002).
5. **Emits no classification hint.** The unit distinguishes nothing.

## The vertical uncertainty is the point

The current unit has no elevation channel. It reports where a target is *around* the beam, never
where it is *within* it. The adapter places the target on the beam centreline and reports a
vertical variance covering the beam's whole elevation extent.

Reporting the centreline without that error bar would be a lie of precision: it would tell the
filter an altitude is known to within the range accuracy when the true altitude spans tens of
metres. The filter would believe it. Nothing in the test suite would notice, because the position
is not wrong — only the claimed certainty is.

The scalar `position_uncertainty_m` fallback is the **largest** of the three axes rather than an
average, for the same reason: a filter that ignores the matrix must not be handed a number that
understates what this sensor does not know.

## Confidence

`Quality.confidence` is a flat value read from configuration. No measured detection statistics
exist for a small airframe against this unit, so there is nothing to compute a per-detection
confidence from, and deriving one from the unit's uncalibrated signal-strength index would
produce something that looks like a measurement and is not. See `docs/radar_testbed.md` for the
campaign that replaces it.

## Health

Mostly a translation of the driver's link state, with one judgement of its own: a link that is
open while nothing decodes is reported DEGRADED and named as such. That is this integration's
most likely first failure — a wrong baud rate opens cleanly, raises nothing, and reports zero
targets forever — and from the watchdog's side it is otherwise indistinguishable from a radar
pointed at empty sky (FM-010).

A reconnecting link is DEGRADED rather than OFFLINE: it is expected back without intervention,
and an operator shown OFFLINE would go looking for a fault that is already resolving itself.
