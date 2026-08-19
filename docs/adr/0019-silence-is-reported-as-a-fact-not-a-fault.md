# ADR 0019 — Silence is reported as a fact, not a fault

**Status:** Accepted
**Context:** FM-010. `backend/system_health`.

## Context

A sensor that produces no detections produces exactly the same evidence whether it has failed or
whether it is watching an empty sky. This is not a gap in the implementation; it is a property
of the observation. No amount of interrogating the sensor resolves it, because the sensor is
working correctly in both cases.

The system nonetheless has to say *something*, because the alternative — an empty map under no
commentary at all — is the failure this whole project is written against: an operator reading a
clear sky off a dead radar.

Two obvious answers are both wrong.

- **Call silence a fault.** The watchdog then reports a fault on every calm night. Operators
  learn within a week that the row means nothing, and the mechanism is disabled precisely when
  it finally matters. This is the same dynamic that sets the alerting thresholds high (ADR 0015).
- **Call silence healthy.** A dead sensor renders green. This is the green light nothing
  computes, and it is worse than having no light.

## Decision

**The watchdog reports the fact and the threshold, and declines to draw the conclusion.**

A source in contact and silent past its policy window is `QUIET` — a verdict about the *data*,
carrying the seconds elapsed and the number it was compared against. It is deliberately not in
the same category as `OFFLINE` or `DEGRADED`, which are verdicts about the *sensor* and are
definite. A `QUIET` source does not degrade the system posture.

Three consequences follow.

1. **Thresholds are site policy, not code.** `config/system_health/policy.json` sets them per
   sensor type, because the right window differs by an order of magnitude between an imaging
   sensor and a radio-frequency one. A single compiled constant would be wrong for both.
2. **The evidence travels with the verdict.** An operator asking why the display says quiet gets
   the seconds and the threshold, not a word they have to take on trust.
3. **Adapters keep making no claim at all.** They report `last_detection_time` and a state about
   themselves. The judgement needs a window and a policy, neither of which an adapter has, and
   putting a threshold in a driver would scatter one decision across every sensor family.

## The verdict settles rather than flaps

Silence measured against a fixed threshold is a sawtooth, so a source whose rate sits anywhere
near its window alternates between two verdicts indefinitely. Measured, before the hysteresis
was added: 34 status transitions in twenty simulated minutes for one working sensor.

A `QUIET` verdict is therefore withdrawn only once the source has sustained health for **more
than one silence window** — the bound is on the field, because a flapping source accumulates
exactly one window of apparent health between crossings and any smaller dwell is satisfied by
the flap itself. The dwell is stated in windows rather than seconds so it scales across sensor
types from one number.

This is the same asymmetry as `AlertPolicy.clear_dwell_s` (ADR 0007) and is chosen for the same
reason: a bad verdict is reached the instant the evidence supports it and withdrawn only once
the source has earned it back. A marginal sensor settles on `QUIET`, which is true of it, rather
than alternating between two verdicts of which one is always wrong.

The dwell applies only to what this module infers. `OFFLINE` and `DEGRADED` are the adapter's
own claims, and a source that has withdrawn its claim is reported back immediately — holding
`BLIND` over a map that is visibly drawing tracks would be a lie in the other direction, and a
driver whose state flaps is a defect that belongs in the driver rather than one to be masked
here.

## Degraded mode declares; it does not act

Entering degraded mode changes nothing about how tracks are assessed. No threshold moves, no
score is adjusted, no alert is suppressed. It is recorded in the log and shown as a banner.

The temptation is to compensate — to lower confirmation thresholds when sensors are down, say.
It is refused because it would mean an operator comparing two incidents was comparing two
different systems, and because the compensating behaviour would be exercised only in the
conditions under which it is least testable.

## The stall is checked from outside

Every component here reports on itself, and a component that has stopped running reports nothing
— indistinguishable from one with nothing to say. So the watchdog timestamps each pass and the
staleness of that timestamp is evaluated at *read* time, against the clock, by a method that
appends nothing. A pipeline that has stopped stepping surfaces as `STALLED`.

Nothing reachable from the API may trigger an assessment. If a request could cause one, the
health endpoint would lose the ability to distinguish a system that is stepping from one that is
merely answering — which is the only thing this check is for.

## An idle system is not a stalled one

An assessment over no sources does not age into `STALLED`. A staleness claim says observations
*stopped*, which needs something to have been observing; a deployment started with no scenario
and no hardware — explicitly supported — never had any. It reports `BLIND`, which is both true
and the more actionable statement, and the wiring assesses once at construction so the first
request is answered from a real judgement rather than from never having run.

Raising a red fault on an ordinary startup is how a watchdog gets muted in its first week, which
would cost more than the check is worth.

## Consequences

- A calm night reads as `NOMINAL` with quiet sources named. That is the intended output.
- A silent-but-contactable sensor that is genuinely broken is reported as quiet, not faulted, and
  the operator makes the call. This is accepted: the system does not have the evidence to make
  it, and pretending otherwise is what this ADR refuses.
- Sensors whose hardware emits frames continuously (the testbed radar does) get the stronger
  guarantee for free — loss of *frames* is loss of contact, which the adapter reports as
  `DEGRADED` or `OFFLINE` on its own evidence, and the watchdog passes that through unchanged.
