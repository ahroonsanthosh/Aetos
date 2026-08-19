# Fusion

**Which tracks are observations of the same physical object.**

Tracking answers *where*. This answers *how many things are there*. It runs between tracking and
classification, so everything downstream — classification, risk, alerting, the API — counts
objects rather than observations.

## The problem

Global nearest neighbour assigns at most one detection per track per cycle, and that is correct:
two detections of *different* objects must never collapse into one track, which is what scenario
H exists to prove. So three sensors watching one drone deliver three detections of it, two go
unassigned, and unassigned detections start new tracks.

Every stage behaves exactly as designed, and an operator sees three contacts where there is one.
That is FM-023, and it was observed rather than theorised.

Loosening the association gate to absorb the surplus would merge genuinely distinct objects — a
far worse failure, and the specific thing ADR 0005 refuses. So reconciliation happens here,
downstream, where being wrong costs a display grouping rather than a corrupted estimate.

## The safety property

**Merging two distinct objects is worse than showing one object twice.**

Over-merging hides a contact, and the one that vanished is the one nobody is watching.
Under-merging is untidy and costs trust but hides nothing. Every threshold below is set with
that asymmetry in mind, and `tests/unit/test_fusion.py` tests it from several directions.

## How two tracks are judged to be one object

Both halves must hold:

| Test | Threshold | Role |
|---|---|---|
| Separation | `separation_m` (150 m) | Permissive. Two radars observing one drone at 4 km routinely disagree by a hundred metres. |
| Velocity | `velocity_mps` (6 m/s) | **Discriminating.** Two aircraft can pass within 150 m — scenario H is built on it — but not while also matching course and speed. |

Plus three guards, each for a case where the pair above stops discriminating:

- **`minimum_detections`** — a track from one return has a position seeded from a single
  measurement and no velocity worth the name. Its apparent agreement is not evidence, and
  admitting it would let clutter attach to a real object and inflate its corroboration.
- **`require_both_moving`** — two hovering objects both have near-zero velocity, so the
  co-moving test passes trivially and separation alone would decide. Two drones loitering a
  hundred metres apart are two drones.
- **`max_object_extent_m`** — see chaining, below.

## Two things that were found by measurement, not by reasoning

**The grouping chattered.** Fragments sit near the merge threshold and drift across it
constantly. With a single threshold, the cycle they merge one track stops being presented and
its alert clears as `track_lost`; the cycle they separate it is presented again and raises a new
one. Measured on `b_approaching_threat`, **one drone produced 34 alerts against the unfused
pipeline's 3.** So merging and splitting have separate thresholds — `split_separation_m`,
`split_velocity_mps` — and the gap between them is a hysteresis band. The same Schmitt trigger
ADR 0007 applies to risk levels, against the same failure.

**Single-link grouping chains.** A joins B, B joins C, and C ends up in A's object however far
from A it is. That chaining is *wanted* between fragments of one drone — sensor A and sensor C
may each agree with B while disagreeing with each other — and catastrophic across a swarm, where
a line of aircraft each within the merge threshold of the next collapses into one enormous
object. Found by pentest: 400 tracks spaced 10 m apart became **one** object spanning 4 km, and
`i_swarm_saturation` produced a genuine object **1497 m** across. `max_object_extent_m` is a hard
cap: a candidate must sit within it of *every* member, and a merge that would breach it does not
happen. Sized from measurement — the widest legitimate fusion across the other eight scenarios
was 327 m.

## What it deliberately does not do

- **Does not re-run association** (`docs/architecture.md`, module map).
- **Does not merge track state.** No member's position, velocity or covariance is altered and no
  averaged position is invented. A covariance-weighted mean of two estimates is statistically
  better than either and is still a position no sensor reported; this system does not display
  those. The primary is *chosen*, so every coordinate on screen is one the tracker produced.
  Estimate-level fusion is a real future step and belongs behind its own decision record.
- **Does not retire anything.** Members stay tracked, stay in the log, keep their own history.
  They are withheld from the presented set, not deleted — `GET /tracks?include_supporting=true`
  returns them.

## Stability, and why it gets this much attention

Two identities have to hold still across cycles or the display lies about continuity.

**The object id.** A group inherits the id its members already carry rather than minting a new
one, so a fourth sensor acquiring the drone does not renumber it. Contested ids — which happen
whenever a group splits, because both halves still carry the old one — are resolved across the
whole cycle at once, to exactly one group: the one holding the incumbent primary, else the one
with most of the object's tracks, else the lowest track id for determinism under replay.

Getting this wrong was the single largest defect in the module. Before it was resolved globally,
**320 of 640 cycles held a duplicated object id and the primary flipped 708 times.**

**Which member is primary.** The incumbent keeps the role while it remains in the group.
Recomputing every cycle would hand it to whichever track is marginally best supported at that
instant, and detection counts cross constantly — so the presented track id would flicker between
fragments of one drone, taking every piece of state keyed by track id with it. Risk hysteresis
is the one that matters: its dwell timers live per track.

## Where the result goes

Written back through the tracker with `apply_fusion`, exactly as classification and risk are, so
the snapshot on every track event carries it and the log rebuilds into the same state (ADR 0006,
FM-025). One owner per piece of track state.

`TRACKS_FUSED` is recorded when a roster changes, not every cycle: the state travels on every
snapshot anyway, and what deserves its own entry is the moment the system decided two contacts
were one.

## Files

| File | Purpose |
|---|---|
| `models.py` | `FusedObject` — one object and the tracks that observe it |
| `policy.py` | Thresholds, the hysteresis band, and the extent ceiling |
| `service.py` | Grouping, identity, primary selection, recording |

Configuration is `config/fusion/policy.json`. See FM-023 and
[ADR 0017](../../docs/adr/0017-track-level-fusion.md).
