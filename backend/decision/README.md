# Decision

**What to put in front of a human.**

The last step in the pipeline, and deliberately the smallest. Risk answers *how dangerous*;
this answers *what should an operator consider doing about it*, which is a policy question
about attention and escalation rather than a measurement.

Keeping them in separate modules means the scoring can be retuned without changing what gets
escalated — and, more importantly, it keeps the strongest output the system can produce in one
small, auditable place.

## The mapping

| Level | Recommendation |
|---|---|
| none, low | `MONITOR` |
| medium | `ALERT` |
| high | `ESCALATE` |
| critical | `RESPONSE_RECOMMENDED` |

Level rather than score, because it is what an operator already reasons in and because it has
hysteresis applied — a recommendation that flapped would defeat ADR 0007 entirely.

Two adjustments, both about evidence rather than severity:

- **Thin evidence becomes `VERIFY`.** An alarming score on three detections of a coasting track
  is a real thing to tell an operator about; it is just not yet a thing to escalate on.
  Downgrading it to `MONITOR` would hide it, leaving it at `ESCALATE` would spend an operator's
  trust on a track the system barely has. `VERIFY` says the useful thing: *look at this and
  find out*.
- **An unassessed track recommends nothing.** Not a finding of any kind (ADR 0011).

## Nothing here acts

Every value is advice to a person. `RESPONSE_RECOMMENDED` means exactly what it says: a
recommendation that a human *consider* a response, decided and carried out by a separate system
with its own authorisation and safety architecture.

Aetos ends at this sentence. There is no activation path anywhere in the repository and no code
that causes anything to happen in the physical world — see `response/README.md`, and
`tests/unit/test_module_boundaries.py`, which scans the codebase for the vocabulary of one and
fails the build if it appears.
