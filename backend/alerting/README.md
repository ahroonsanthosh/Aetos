# Alerting

Turns risk levels into **alerts**: findings with an identity, a lifecycle, and a claim on an
operator's attention that outlives the cycle that produced them.

## Why this is not part of risk

A risk score is a property of one moment. It is recomputed every cycle, replaced without
ceremony, and gone the instant the geometry changes. That is correct for an assessment and
useless as a record of attention: nothing in a stream of scores can express *this was raised,
and nobody looked at it*.

An alert can. It is created once, persists across cycles, survives a restart, and ends only
when something ends it. Keeping the two modules apart means a change to what counts as
alertable — a stadium tightening its threshold before an event — cannot quietly change what
counts as dangerous.

The dependency runs one way and through the data, not the code. `AlertService.review_cycle`
reads `track.threat`, which risk has already written back, on exactly the same terms the API
reads it. Alerting imports no risk internals and has no access to the engine, so it cannot
start second-guessing a score it disagrees with.

## What an alert is

| Field | Meaning |
| --- | --- |
| `raised_level` | The level that crossed the threshold |
| `peak_level` | The worst level reached while open. **Never falls** |
| `level` | The level as of the latest cycle. Falls freely |
| `codes` / `reasons` | Risk's vocabulary, carried through unchanged |
| `state` | `active`, `acknowledged`, or `cleared` |

`peak_level` and `level` are separate because an alert that touched CRITICAL and subsided is
not retrospectively a MEDIUM one. An operator triaging a feed needs to know what each incident
was at its worst, not only what it is right now.

Alerting invents no vocabulary of its own for *why* something is dangerous. Every code and
sentence on an alert came from risk. An alert that cannot say why it fired leaves an operator
with two options — obey or dismiss — and the second is the one they will learn to take.

## Lifecycle

```
                 level >= raise_at
     (none) ─────────────────────────► ACTIVE ──────────────► ACKNOWLEDGED
                                         │  ▲                      │
                                         │  └──────────────────────┘
                                         │     escalation withdraws
                                         │     the acknowledgement
                level <= clear_at_or_below
                for clear_dwell_s, or track lost
                                         ▼
                                      CLEARED
```

**Acknowledged is not cleared.** Acknowledging says a human has seen the alert; clearing says
the condition has ended. Only the assessed risk subsiding, or the track being lost, ends an
alert — there is deliberately no API for an operator to clear one. Collapsing the two would
let a busy operator dismiss a still-approaching aircraft and leave no record that the threat
outlived the dismissal.

**Escalation withdraws an acknowledgement.** An acknowledgement attests that someone saw the
alert *as it then stood*. If the situation gets worse, carrying it forward would let a HIGH
someone glanced at become a CRITICAL nobody has looked at, still displaying as handled.

**Clearing distinguishes its reasons.** `risk_subsided` and `track_lost` are different facts.
An aircraft that flew out of coverage has not been dealt with, and a feed that renders the two
identically invites the reader to assume the reassuring one.

## Hysteresis, and why there are two thresholds

`raise_at` is HIGH and `clear_at_or_below` is MEDIUM, leaving a band where an alert neither
raises nor clears. Without that gap a track sitting on the boundary would raise and clear on
alternating cycles.

On top of the band, clearing requires `clear_dwell_s` of *continuous* time below the
threshold — not twenty seconds of dips adding up. A threat briefly occluded behind a building,
or one whose score dips for a cycle as the filter re-converges, must not clear the alert and
then raise a second one seconds later; the operator would read two separate incursions where
there was one.

Raising has no dwell at all. The asymmetry is deliberate: alerts rise immediately and fall
slowly. A genuine threat must never be held back by a timer.

Note this is the *second* layer of hysteresis. Risk already de-flaps the level itself
(ADR 0007). This one exists because the question is different — "has the level changed" and
"is this still worth interrupting a human about" have different right answers, and the second
tolerates far more lag than the first.

## Durability

Alerts are rebuilt from the event log at startup (`AlertService.rebuild`). The log is the
authority (ADR 0006); the in-memory set is a projection of it.

This is not a nicety. An alert that vanished because the process bounced is indistinguishable,
to the operator, from one that was dealt with. Rebuilding also resumes the identifier
generator above the highest ID the log contains — a second `ALT-00007` would make two distinct
alerts indistinguishable in the record, and an acknowledgement of one appear to acknowledge
the other.

## Configuration

`config/alerting/policy.json`. Thresholds and dwell are a deployment's judgement about what
warrants an interrupt, made by people who will never read this directory.

## What this module does not do

It does not decide a response, recommend one, or reach anything that could act. The most
severe thing an alert can do is appear on a screen with reasons attached. Response remains a
separate system with its own authorisation and safety architecture, and nothing here is
capable of engaging one.
