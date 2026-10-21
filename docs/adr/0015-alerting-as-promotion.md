# ADR 0015 — Alerting as promotion, not assessment

**Status:** Accepted
**Date:** 2026-08-10
**Supersedes:** nothing. Fills the gap `GET /alerts` documented as "build stage 8".

## Context

Risk scores every track every cycle and emits `RISK_CHANGED` when the level moves (ADR 0013,
ADR 0007). That is an assessment, and it is complete. What it cannot express is *attention*.

A score is a property of one moment: recomputed each cycle, replaced without ceremony, gone
the instant the geometry changes. Nothing in a stream of scores can say **this was raised, and
nobody looked at it** — the fact an incident review turns on. Nor can it survive a restart: a
process bounce silently discharges every concern the system had, and the operator cannot tell
that from a quiet sky.

Before this ADR the alert feed was empty, the endpoint scanned the log for events nothing
appended, and the page explained the emptiness by saying risk assessment had not been built —
a statement that was true when written and became false when risk landed, without anyone
noticing for the whole period in between.

## Decision

Alerting is a **separate module that promotes risk levels**, consuming assessed tracks rather
than the risk engine.

### 1. Its input is `track.threat`, not the engine

`AlertService.review_cycle` reads the assessment risk has already written back, on exactly the
same terms the API reads it. Alerting imports no risk internals.

The alternative — letting alerting call the engine, or subscribe to its internals — would give
it the means to disagree with a score. It would then be a second assessor, and two assessors
that can disagree eventually do, at which point the operator has two numbers and no way to
choose. Keeping the dependency in the data means a change to what counts as *alertable* cannot
change what counts as *dangerous*.

### 2. Acknowledged is a state, distinct from cleared

Acknowledging records that a human saw the alert. Clearing records that the condition ended.
There is deliberately **no API for an operator to clear an alert**: only the assessed risk
subsiding, or the track being lost, ends one.

Collapsing the two would let a busy operator dismiss a still-approaching aircraft and leave no
record that the threat outlived the dismissal. The asymmetry is the point — an operator can
say "seen", never "over".

### 3. Escalation withdraws an acknowledgement

An acknowledgement attests that someone saw the alert *as it then stood*. When the level rises
above the alert's peak, the acknowledgement is dropped and `ALERT_ESCALATED` recorded.

Without this, a HIGH someone glanced at becomes a CRITICAL nobody has looked at, still
displaying as handled. That is a worse failure than never having acknowledged it at all,
because it actively misinforms.

`ALERT_ESCALATED` is a distinct event rather than a second `ALERT_CREATED`: it is the same
incident getting worse, and two creations would tell a reviewer two aircraft came at them.

### 4. Two thresholds and a dwell — a second layer of hysteresis

- `raise_at` (HIGH) and `clear_at_or_below` (MEDIUM), leaving a band where an alert neither
  raises nor clears. Without the gap a track on the boundary raises and clears alternately.
- Clearing additionally requires `clear_dwell_s` of **continuous** time below the threshold,
  not dips adding up. A threat briefly occluded, or one whose score dips for a cycle as the
  filter re-converges, must not clear and re-raise — the operator would read two incursions
  where there was one.
- Raising has no dwell. Alerts rise immediately and fall slowly; a genuine threat must never
  be held back by a timer.

This is deliberately a second layer on top of ADR 0007's hysteresis on the level itself. The
questions differ — "has the level changed" and "is this still worth interrupting a human" have
different right answers, and the second tolerates far more lag.

`raise_at` is HIGH rather than MEDIUM because an alert is an interrupt. The cost of firing too
readily is not noise but habituation: operators learn to dismiss the feed, which disables the
mechanism exactly when it matters. MEDIUM is already visible on the track list and the map.

### 5. Alerts are rebuilt from the log at startup

`AlertService.rebuild()` replays `ALERT_CREATED` / `ALERT_ESCALATED` /
`OPERATOR_ACKNOWLEDGED` / `ALERT_TRANSFERRED` (ADR 0021) / `ALERT_CLEARED` and reconstructs the open set. The log remains the
authority (ADR 0006); the in-memory set is a projection.

The identifier generator resumes above the highest ID in the log. A second `ALT-00007` would
make two distinct alerts indistinguishable in the record, and an acknowledgement of one appear
to acknowledge the other.

### 6. `GET /alerts` reads the service, not the log

The previous endpoint scanned a bounded window of the log per request. An alert raised ten
thousand events ago — precisely the long-running one that matters most — would fall outside
the window and vanish from the feed with no error.

The response carries `assessing`, so an empty list can distinguish "nothing is wrong" from
"nothing is watching". Those are the same shape on the wire and opposite in meaning (ADR 0011).

## Consequences

**Good.** An alert now has an identity that survives restarts and a lifecycle an incident
review can reconstruct from the log alone. The feed cannot silently drop old alerts. Every
alert carries risk's reason codes unchanged, so it can be interrogated rather than only obeyed
or dismissed.

**Costs.** A second hysteresis layer means a track can be HIGH on the track list while no alert
is open — during the clear dwell, or before the level was reached. That is intended, and the
track list remains the complete picture; the alert feed never claims to be.

Alerting holds in-memory state and is therefore the first module besides the projection whose
correctness depends on replay being right. `test_alerting.py` covers rebuild directly,
including that cleared alerts stay cleared and identifiers are not reused.

**Explicitly out of scope.** Nothing here decides a response, recommends one, or reaches
anything that could act. The most severe thing an alert can do is appear on a screen with
reasons attached. Response remains a separate system with its own authorisation and safety
architecture, and this build contains no code path capable of engaging one.
