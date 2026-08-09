# `response/` — future response integration. Not implemented.

**This directory contains no code and must not contain code until the design pass described
below has been done.**

## What this is

A structural placeholder for a future response-integration layer, so that adding it later is
an extension of the architecture rather than a redesign of it. It exists to hold a shape, not
a capability.

```
orchestration/   empty — routing logic, once authorised
adapters/        empty — per-system adapters, later
```

## What Aetos does today

The pipeline ends at a recommendation presented to a human:

```
detection → track → classification → threat assessment
          → recommendation → HUMAN DECISION → (future) response integration
```

The most severe output the system can produce is the recommendation category
`RESPONSE_RECOMMENDED`. That is **advice to an operator** and nothing more. No code anywhere
in this repository can act on it, and the frontend never talks to this directory — everything
routes through `backend/decision/` and stops there.

A test (`tests/unit/test_module_boundaries.py`) fails the build if activation vocabulary
appears in `backend/` or `sensors/`, and an API test asserts no route exposes anything acting
on the physical world. Those are guards, not the reason — the reason is that this is a
sensor-fusion and decision-support product, and the boundary is architectural.

## Why it is empty rather than stubbed

A stub invites completion. Empty directories with this README do not.

Response execution is a genuine future capability for this project — not a permanently
disabled feature, and not something being quietly deferred. But it is the part of the system
where a defect has physical consequences, and it cannot be a side effect of a UI sprint or a
backend refactor.

## What has to happen before any code goes here

A dedicated design pass, done deliberately and separately, covering at minimum:

- **Authorisation** — who may authorise an action, how that authority is established and
  verified, and how it is revoked. Aetos currently has *no authentication at all*
  (`docs/failure_modes.md` FM-001); operator actions are attributable in structure but not
  authenticated in fact. That gap alone disqualifies this layer from being built today.
- **Safety** — interlocks, geographic and temporal constraints, abort paths, and the failure
  behaviour of every one of them. What happens when the network drops mid-action is a design
  question, not an implementation detail.
- **Audit** — an evidential record of who authorised what, when, on what information, and
  what followed. The event log (ADR 0006) is the right substrate and is deliberately
  immutable, but it currently proves *what the system did*, not *who told it to*.
- **Legal and regulatory review** — jurisdiction-specific and outside what this repository
  can settle on its own.

Until that work exists as its own decision record, this directory stays empty.

## For a future maintainer

If you have arrived here intending to add orchestration code because a feature needs it:
that is exactly the path this README exists to interrupt. The correct next step is the design
pass, and a new ADR recording its outcome — not a first commit in `orchestration/`.
