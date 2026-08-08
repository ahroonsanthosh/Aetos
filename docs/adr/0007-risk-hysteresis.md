# ADR 0007 — Risk level hysteresis: dual thresholds plus minimum dwell

- **Status:** Accepted
- **Date:** 2026-08-08

## Context

Spec §10 requires hysteresis on risk threshold crossings so that a score sitting near a
boundary does not flap between levels on every update cycle and spam the operator with
alerts. It does not specify the mechanism.

This matters more than it sounds. An object holding station just outside the protected zone
is a realistic and important case, and it is exactly the case that produces a score
oscillating around a threshold. An operator who receives forty alerts for one object stops
reading alerts — the failure mode is operator desensitisation, which is worse than the
original flapping.

## Decision

Risk level transitions use two independent mechanisms, both of which must permit a change:

1. **Dual thresholds (Schmitt trigger).** Each level boundary has a separate rise threshold
   and fall threshold, with the fall threshold lower. Escalating from MEDIUM to HIGH requires
   crossing the higher rise threshold; de-escalating back requires dropping below the lower
   fall threshold. The gap between them is the hysteresis band, configured per boundary.
2. **Minimum dwell time.** After any level change, the level is held for a configured
   minimum duration before another change is permitted, measured on the injected clock
   (ADR 0003).

**Escalation is not exempt but is asymmetric.** Dwell time for escalation is configured
substantially shorter than for de-escalation. A genuine threat that escalates quickly must
not be held back by a de-escalation timer, but neither should a single noisy update promote
a benign object.

The underlying `risk_score` is **not** smoothed. It is reported raw. Hysteresis applies only
to the discrete `risk_level` and therefore to alerting. An operator looking at the score sees
what the engine actually computed this cycle.

Every level change emits a `RISK_CHANGED` event carrying both the old and new level, the
score, and the reason codes that drove it.

## Rationale

Two mechanisms rather than one because they solve different problems. Dual thresholds handle
a score drifting slowly across a boundary. Minimum dwell handles a score jumping wildly
across a wide band — which dual thresholds alone do not stop.

Leaving the score unsmoothed keeps the engine honest. Smoothing the score would make the
displayed number disagree with the reason codes attached to it, and the reason codes are the
part an operator is meant to be able to act on.

## Consequences

- Risk assessment becomes stateful per track. It needs the previous level and the time of
  the last transition. This state is part of the track record and is rebuilt on replay from
  the event log, so replayed risk decisions match live ones exactly.
- There is a deliberate lag between a score crossing a boundary and the level following.
  Sized by configuration, visible in the reason codes, and tested at the boundary — the
  threshold-boundary hysteresis test in spec §17 is the acceptance test here.
- Configuration lives in the site/environment config, never inline, so thresholds can be
  tuned per deployment without a code change.
