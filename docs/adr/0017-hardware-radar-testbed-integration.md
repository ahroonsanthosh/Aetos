# ADR 0017 — Integrating a testbed radar behind a second, inner interface

- **Status:** Accepted
- **Date:** 2026-08-14

## Context

A physical radar unit is now on the bench and needs to be integrated: a 24 GHz FMCW module
with vehicle-detection firmware, reached over a USB-to-TTL serial adapter. Its full
specification and wiring are in `docs/radar_testbed.md`.

Three facts about it drive everything below.

**It is explicitly a placeholder.** It was bought to have something real to develop against,
not because it is the sensor Aetos will ship with. It will be replaced, and the replacement is
unlikely to resemble it.

**It is mis-tuned for the target.** The firmware is written for vehicle-scale radar cross
sections and is rated to 100 m against them. Its behaviour against a 135 g, 25 cm airframe is
not merely uncertain — nobody has measured it. Detection sensitivity and the
consecutive-detection threshold have to be swept, not chosen.

**It cannot measure elevation.** It reports range, an angle in one plane, and a range rate. The
only way to look upward is to tilt the whole board, which rotates the beam and trades
ground-level coverage for elevated coverage. One fixed tilt scans one fixed slice of sky.

Aetos already has an interface for sources of detections: `SensorAdapter`, producing canonical
`Detection` objects, which is what makes the simulator and a real sensor indistinguishable to
everything downstream (ADR 0002). The question this record answers is whether that interface is
sufficient here.

## Decision

**1. Two nested interfaces, not one.**

    hardware -> RadarSensor -> HardwareRadarAdapter -> Detection -> Aetos

`SensorAdapter` stays exactly as it is and remains the boundary the rest of the system knows
about. Below it sits a second contract, `RadarSensor` (`sensors/radar/radar_interface.py`),
which a physical radar unit implements: `connect`, `disconnect`, `get_targets`, `get_status`,
`configure`. It carries `TargetDetection` — a range, an off-boresight angle, an optional
elevation, a range rate — in the sensor's own frame.

**2. Model-specific code is confined to two modules,** `sensors/radar/ld2451_protocol.py` (byte
layout, framing, command words) and `sensors/radar/ld2451_driver.py` (link management, polling,
configuration, health). A hardware swap replaces those two and adjusts `config.py`. The
geometry, the adapter, and everything downstream are untouched.

**3. The confinement is a test, not a convention.**
`tests/unit/test_module_boundaries.py` fails the build if the unit's model designation appears
in any module outside those two, and separately if the adapter imports a concrete driver.

**4. The unit's inability to measure elevation is expressed as uncertainty, not filled in.**
`TargetDetection.elevation_deg` is `None`. The adapter places the target on the beam centreline
and reports a 3x3 ENU covariance whose vertical term is the beam's own elevation extent, as a
uniform distribution: `sigma = extent / sqrt(12)`.

**5. Mount tilt is configuration and is recorded with every session.** `mount.tilt_deg` is
applied in `sensors/radar/geometry.py` and written into the header of every raw log.

**6. Detection confidence is a flat configured number until measurements exist.** No
per-detection confidence is derived from the unit's uncalibrated signal-strength index.

**7. Protocol constants are treated as unverified.** The frame headers, tails, command words and
field offsets come from secondhand descriptions rather than the vendor document. They are
gathered into named constants at the top of one file, flagged, and the decoder counts every byte
it discards and every frame it rejects rather than repairing either.

## Rationale

**Why a second interface rather than making the driver emit `Detection` directly.** A driver
that produced canonical Detections would have to know the site's local frame, the WGS-84 datum,
and the mount geometry, and would have to mint detection identifiers. Every future driver would
reimplement all of it. Worse, it would put the decision of *what altitude to report* inside the
component least equipped to make it honestly — and the correct answer for this unit is "I do not
know, and here is how much I do not know", which is a statement about uncertainty that belongs
where the covariance is assembled.

The cost is one more layer. That is worth paying for hardware whose replacement is a matter of
when, not if.

**Why the covariance matters more than the position.** A target 100 m out in a 30-degree beam
has an altitude spanning roughly 50 m. An integration that reported the centreline without an
error bar would tell the Kalman filter that altitude is known to within the range accuracy. The
filter would weight it accordingly and produce confident nonsense, and nothing in the test suite
would notice, because the position itself is not wrong — only the claimed certainty is. This is
FM-004 arriving through the uncertainty channel instead of the datum channel.

**Why the model name is banned outside two files, when ADR 0002's vendor rule already exists.**
The existing rule bans vendor names outright, which works because no vendor code exists. A
driver must name its device — that is what a driver is — so a blanket ban here would be
unenforceable and would be deleted the first time it got in the way. Confinement is the rule
that is both enforceable and load-bearing: the day `ld2451` appears in the tracker, the hardware
is no longer swappable, and that is worth a red test.

**Why unverified constants are surfaced rather than smoothed over.** The most likely first
failure of this integration is a wrong baud rate or a wrong frame constant, and its symptom is a
link that opens cleanly, raises nothing, and reports zero targets forever — indistinguishable
from a radar watching an empty sky. So the driver counts discarded bytes separately from
malformed frames, and the adapter reports DEGRADED, by name, when frames are arriving and none
are decoding. That is the difference between an afternoon of debugging and a week of it.

## Consequences

- `SensorType.RADAR` now has an implementation. It remains a testbed: the confidence it reports
  is configured rather than measured, and no detection logic should be built on it until the
  campaign in `docs/radar_testbed.md` has produced data.
- `pyserial` becomes an optional dependency (`pip install -e ".[hardware]"`), imported inside
  `open()` so the package imports without it.
- The adapter emits no classification hint at all, so classification for radar-only tracks rests
  entirely on kinematic evidence. That is correct — the unit distinguishes nothing — and it is
  what ADR 0002 built the classification module to cope with.
- Tracks fed only by this unit will have large vertical uncertainty and will converge slowly in
  altitude. That is the honest consequence of a sensor with no elevation channel, and it should
  not be tuned away.
- Elevation coverage remains a mechanical question. A second unit at a different tilt, or a
  motorised sweep mount, is out of scope; `mount.tilt_deg` only lets the software interpret
  whichever slice is being scanned.

## Alternatives considered

- **Implement `SensorAdapter` directly in the driver, no inner interface.** Fewer files. Rejected:
  it makes the next hardware change a rewrite of geodesy and identifier logic rather than a
  swap, and it puts the altitude decision in the wrong component.
- **Report elevation as zero with a small uncertainty.** Simplest, and every downstream module
  would work immediately. Rejected outright: it is a fabricated measurement, and the filter would
  believe it. This is the single decision in this record with the largest gap between "easy" and
  "correct".
- **Derive per-detection confidence from the signal-strength index.** Tempting — the byte is
  right there. Rejected: it is uncalibrated, unvalidated against the intended target, and would
  look exactly like a measurement to the risk engine. A flat number that is honest about being
  flat is a stronger claim than a scaled one that is not.
- **Ban the model name entirely, as ADR 0002 bans vendor names.** Rejected as unenforceable; see
  the rationale.
- **Wait for the production radar and integrate nothing now.** Rejected: the value of this unit
  is that it makes serial framing, reconnection, mount geometry, honest covariance and health
  reporting real problems to be solved now, against hardware, rather than assumptions to be
  discovered later.
