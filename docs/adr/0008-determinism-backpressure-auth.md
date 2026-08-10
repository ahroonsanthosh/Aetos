# ADR 0008 — Three gaps in the specification: determinism, backpressure, authentication

- **Status:** Accepted
- **Date:** 2026-08-08

## Context

Three requirements are implied by the specification but never stated. Each is cheap now and
expensive later.

## Decision

### 1. Determinism — seeded randomness

The simulator is stochastic: measurement noise, probability of detection, false alarms and
classification confusion are all random processes. Unseeded, a failing scenario test cannot
be reproduced, and replay cannot reproduce the run it claims to replay.

Every scenario declares an explicit RNG `seed`. All randomness in the simulator draws from a
generator derived from that seed and the sensor ID, so adding a sensor to a scenario does not
perturb the noise sequence of existing ones. The seed is recorded in the `SCENARIO_STARTED`
event. A scenario run is reproducible from its seed alone.

### 2. Backpressure — bounded queues with an explicit shed policy

Scenario I (swarm/saturation) exists to find the point where the system cannot keep up.
Without a defined policy, "cannot keep up" manifests as unbounded memory growth followed by
a crash, which is not a degraded mode — it is an outage.

Every inter-stage queue is **bounded**, with capacity from configuration. On overflow the
policy is:

- **Never silently drop.** Every shed detection emits an event recording that it was shed.
- **Shed the newest low-value input first** — detections not gated to any existing track,
  lowest quality first. Established confirmed tracks keep receiving updates under load;
  what degrades first is the ability to acquire *new* tracks.
- **Sustained shedding activates degraded mode** (spec §16), which is visible in the UI.

This makes saturation a defined, observable, testable state rather than an emergent one.

### 3. Authentication — stubbed behind an interface, deliberately not half-built

> **Superseded by [ADR 0016](0016-authentication-and-authorisation.md) (2026-08-10).** The
> reasoning below stands and is left unedited: it is why authentication was not built earlier,
> and the decision to build it followed from the deployment changing rather than from the
> argument being wrong. §1 and §2 of this record are unaffected.

Spec §14 lists a `users` table. No authentication or authorisation requirement is stated
anywhere else: no roles, no session model, no statement of who may acknowledge an alert.

A half-built authentication system is worse than none, because it looks like security. The
decision is to define an `Identity` interface and an `OperatorContext` carried on every
operator action — so that `OPERATOR_ACKNOWLEDGED` events are attributable from day one — and
back it in V0 with a single configured local operator identity, with **no login and no
authorisation checks**.

This is recorded prominently in `docs/failure_modes.md` as a known and accepted gap. Aetos
in its current state must not be exposed to an untrusted network.

## Consequences

- Scenario definitions gain a required `seed` field.
- Queue capacities and shed thresholds become configuration, and the shed path needs its own
  event type and its own test.
- The audit trail is attributable in structure but not authenticated in fact. Any claim that
  the event log proves *who* did something is not yet supportable, and the failure-modes
  document says so in those terms.
