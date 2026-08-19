# ADR 0017 — Multi-sensor reconciliation happens on tracks, not on detections

**Status:** Accepted
**Date:** 2026-08-19
**Closes:** [FM-023](../failure_modes.md)

## Context

One physical object observed by three sensors became three concurrent CONFIRMED tracks. This was
observed, not theorised: running `b_approaching_threat` against the shipped test site produced
three tracks at the same range and bearing with consistent kinematics, for one declared object.

The cause is not a defect in any stage. Global nearest neighbour assigns at most one detection
per track per cycle (ADR 0005), which is correct — two detections of *different* objects must
never collapse into one track, and scenario H exists to prove it. Three sensors deliver three
detections of one drone; two go unassigned; unassigned detections start new tracks. Every step
behaves as designed and an operator counts three contacts.

The consequence compounds. Under a swarm the multiplier is the sensor count, which brings the
saturation ceiling forward; risk scores each fragment separately; alerting raises an alert per
fragment; and the situation pass had to carry its own stand-in clustering to avoid reporting one
drone as a four-axis coordinated approach.

## Decision

**Reconciliation is a distinct stage between tracking and classification, operating on tracks.**

Three placements were available and two were rejected.

**In association.** Rejected, and explicitly so in FM-023's own text: loosening the gate to
absorb the surplus detections would merge genuinely distinct objects. That is a far worse failure
than showing one object twice, and it is the failure ADR 0005 is built to prevent.

**On detections, before tracking.** Rejected as the larger change to the riskier place. Merging
measurements requires a fused covariance and tight cross-sensor time alignment, and getting it
wrong corrupts the estimate rather than the display. It also puts a second estimator upstream of
the filter, where a mistake is hard to see.

**On tracks, after tracking.** Chosen. It matches the pipeline `docs/architecture.md` already
declared, it cannot corrupt an estimate because it changes none, and a mistake costs a display
grouping that an operator can see and interrogate.

### What it may not do

- **Not re-run association**, per the module map's "must not" column.
- **Not merge track state.** A covariance-weighted mean of two estimates of one object is
  statistically better than either, and it is still a position no sensor reported. The rest of
  this system refuses to display those and a fusion module is a bad place to make the first
  exception. The presented track is *chosen* from the members, so every coordinate on screen is
  one the tracker produced. Estimate-level fusion is a genuine future step and needs its own
  record.
- **Not retire anything.** Members stay tracked, stay in the log, keep their history. They are
  withheld from the presented set, not deleted, and `GET /tracks?include_supporting=true` returns
  them.

### The safety property

**Merging two distinct objects is worse than showing one object twice.** Over-merging hides a
contact and the one that vanished is the one nobody is watching. Under-merging is untidy and
hides nothing. The thresholds are asymmetric because the failures are: separation is permissive
(150 m — two radars observing one drone at 4 km routinely disagree by that much) and velocity is
strict (6 m/s), because two aircraft can pass within 150 m but not while also matching course and
speed.

## Consequences

`backend/risk/correlation.py` no longer clusters. It was always described as a stand-in to be
deleted once fusion supplied the answer properly, and leaving it would have been worse than
redundant: fusion holds a group together across a hysteresis band, so two objects it deliberately
keeps apart can sit closer than a single-threshold test would merge them. Two subsystems
answering "are these the same object" differently, in front of the same operator, is worse than
either answer alone.

`GET /tracks` returns one entry per object by default. FM-023's interim mitigation said the
interface must not de-duplicate, *because suppressing them would conceal a real limitation* —
that reasoning ends when the limitation does, and the roster is on the primary for anyone who
asks.

## Two things measurement found that reasoning did not

**Identity had to be resolved globally, not per group.** When a group splits — a fragment
drifting outside the gate for a few cycles, which happens constantly — both halves still carry
the old object id and both inherited it. Two groups then held the same identity, the
incumbent-primary record was overwritten by whichever was processed last, and the presented track
flipped every cycle. Because alerts key on the presented track, each flip cleared one alert and
raised another. Measured on `b_approaching_threat`: **320 of 640 cycles held a duplicated id and
the primary flipped 708 times.** A contested id now goes to exactly one group.

**The grouping needed hysteresis, and an extent ceiling.** Fragments sit near the merge threshold
and drift across it, so a single threshold made the grouping chatter — one drone produced 34
alerts against the unfused pipeline's 3. Merging and splitting therefore have separate
thresholds, the same Schmitt trigger ADR 0007 applies to risk levels. Separately, single-link
grouping chains: 400 tracks spaced 10 m apart collapsed into one object spanning 4 km, and
`i_swarm_saturation` produced a genuine object 1497 m across against 327 m for the widest real
fusion anywhere else. `max_object_extent_m` caps it hard, because under-counting is the dangerous
direction.

With both in place, alert volume fell below the unfused baseline on every scenario — 3 → 1 on
`b_approaching_threat`, 54 → 28 on `i_swarm_saturation` — and object counts matched the declared
truth of all nine.

## What this does not fix

Fusion reconciles what tracking produced; it does not recover an object tracking never saw. And
because the presented track is one member's estimate rather than a combination, the position
shown is as good as the best single observation, not better than it. That is the deliberate
trade above and the obvious place to improve next.
