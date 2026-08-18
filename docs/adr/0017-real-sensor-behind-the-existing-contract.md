# ADR 0017 — Real hardware enters behind the existing adapter contract

**Status:** Accepted
**Date:** 2026-08-15
**Supersedes:** nothing. Discharges the claim ADR 0002 has been making since the first commit.

## Context

A physical radar arrived: a 24 GHz FMCW module on a UART, through a USB-to-TTL adapter. It is
explicitly a testbed unit, bought to learn on and to be replaced by production-grade hardware
later.

ADR 0002 has claimed from the beginning that a real sensor would be an *addition* to
`sensors/adapters/` and a change to nothing else. Until now that was an untested claim: every
detection in the system came from the simulator. This is the first opportunity to find out
whether the narrow waist actually holds, and the first opportunity to breach it by accident.

The hardware brings three problems the simulator never posed.

**It cannot measure elevation.** Not "does not by default" — there is no elevation channel and
no configuration that produces one. Elevation exists only as the mechanical tilt of the mount,
which rotates the whole ±20° cone. The canonical `Detection` requires an altitude.

**Almost nothing about it is verified.** Published sources disagree on its baud rate. The
report payload layout differs between sources. Its rated 100 m range is against vehicle-scale
radar cross-section, and its firmware is tuned for vehicles; whether it sees a 135 g airframe
at any range is unknown.

**It will be thrown away.** Any structure that makes its removal a migration rather than a
deletion is a structure that will still be here after the hardware is gone.

## Decision

### 1. It implements `SensorAdapter`. There is no second sensor interface.

The obvious alternative was a dedicated radar abstraction — a `RadarInterface` with
`connect` / `disconnect` / `get_targets` / `get_status` / `configure`, and a `TargetDetection`
model carrying range, azimuth, an optional elevation and an optional confidence, with the rest
of the pipeline consuming it. It is a reasonable design in isolation. It is the wrong one here,
for a specific reason.

Aetos already has that abstraction. `SensorAdapter` is `connect`/`disconnect`/`get`/`status`
under different names, and `Detection` is `TargetDetection` with the geodesy done and the
uncertainty modelled. Adding a second one would give the system **two narrow waists**, and
something would have to translate between them — a translator that is the exact seam ADR 0002
exists to prevent. `elevation_deg: None` in a parallel model is also strictly worse than what
the canonical schema can already say: `None` states that a field is absent, while a covariance
states *how much we do not know*, which is what a filter can actually use.

So: one interface, and hardware peculiarity lives inside the implementation. The five methods
of the rejected design map cleanly onto the four that exist, plus a `configure()` that is the
adapter's own and appears nowhere in any downstream call.

### 2. Elevation is beam-centre plus honest variance, never silence and never a guess

The adapter emits an altitude computed from the configured mount tilt — the beam centre, which
is the best point estimate that exists — and a `position_covariance_enu` whose vertical
variance is the full elevation ambiguity of the beam treated as a uniform spread. At every
range the vertical variance is several times the horizontal, so the filter discounts the
altitude channel instead of believing it.

The alternatives were both worse. Emitting the altitude with the isotropic scalar only would
make the tracker confidently wrong about height, which is precisely the failure `Measurement`'s
covariance field exists to prevent (FM-004, FM-051). Refusing to emit at all would discard
range and azimuth, which the sensor *does* measure, and leave the operator with nothing.

`mount_tilt_deg` is therefore configuration with downstream meaning, and it is recorded per
test session rather than assumed constant.

### 3. The adapter states what the hardware cannot support, by omission

No classification hint — the firmware is vehicle-tuned and has no basis for a statement about a
small airframe, so it makes none rather than handing the classifier evidence that was invented
here (FM-052). No `snr_db` — the signal-quality byte is an uncalibrated integer, and reporting
it in decibels would be a unit claim. Confidence derived from that byte is capped well below
1.0 until test data exists.

An adapter is the last place in the system that knows what the sensor actually measured. Every
claim it does not make here is a claim nothing downstream can make either.

### 4. Bytes come through a `ByteLink`, so `pyserial` is optional and sessions are replayable

The adapter is written against a four-method byte pipe, not against a serial port. `pyserial`
is an optional dependency imported at point of use, the whole test suite runs with no hardware
and no extra installed, and a captured byte log replays through the real parser — which is the
only way to check the driver against what the sensor said rather than what we assumed it says.

### 5. Failure is a health state, silence is never the answer

An unreachable sensor reports OFFLINE with the reason in words and retries on a backoff. A
byte stream that never syncs — the signature of a wrong baud rate, the most likely bring-up
mistake — reports DEGRADED naming the configured baud rate. Malformed frames are counted and
discarded, never raised. What the adapter must never do is return an empty list because
something broke, because an empty list means an empty sky (FM-010).

## Consequences

The claim in ADR 0002 is now load-bearing rather than aspirational: a real sensor was added and
no module downstream of ingestion changed, which the existing suite proves by continuing to
pass untouched.

Run mode LIVE remains **not built**. A driver is not an integration: this adapter is not
constructed by `backend/main.py`, is not referenced by any site configuration, and nothing it
reports has been validated against a real airframe. The bring-up sequence that would change
that is in the package README, and it ends with raw data, not with detection logic.

Swap-out day is a deletion of one directory and its two test files.

## References

- `sensors/adapters/uart_fmcw_radar/README.md` — hardware, wiring, bring-up sequence
- ADR 0002 — detection schema and provenance
- ADR 0003 — injectable clock
- `docs/failure_modes.md` — FM-050, FM-051, FM-052
