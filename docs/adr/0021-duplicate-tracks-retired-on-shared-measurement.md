# ADR 0021 — Duplicate tracks are retired when they share one stream of measurements

**Status:** Accepted
**Relates to:** [ADR 0005](0005-gnn-association.md) (association), [ADR 0020](0020-fusion-on-detections.md) (fusion), FM-023

## Context

Fusion (ADR 0020) stops most multi-sensor duplicates from forming, but not all. Measured on the
live server: two radars' detections of one drone occasionally land just outside the gate while a
broad sensor gates with both, fusion's ambiguity rule fuses none of them, and the surplus
detection starts a second track. From then on the two tracks **take turns** winning the single
measurement each cycle, so neither goes stale. On `b_approaching_threat` the drone showed as two
tracks for most of the run.

Nothing in the tracker could end that. Tracks end only by going unsupported, and turn-taking
keeps both supported indefinitely.

The obvious cure — merge two tracks that are close and moving alike — is the operation that
hides a contact. Two drones flying in formation are close and moving alike. An operator shown
one track where there are two has been told the sky is emptier than it is, and nothing on the
display contradicts it. That is the worst error this system can make, and it is why the cure
was held back from the fusion port as a decision rather than slipped in.

## Decision

**Two tracks are judged to be one object by the pattern of their updates, not by their
proximity.**

- **Proximity is necessary, never sufficient.** The pair must be statistically one state —
  position *and* velocity, squared Mahalanobis distance under the summed covariances, at the
  association gate's own confidence on six degrees of freedom.
- **What decides is whether they share measurements.** One object seen twice feeds both tracks
  from one stream, so they take turns: the measured duplicate shared an update in 0 of 10
  cycles. Two real objects each produce their own detections every cycle — and fusion never
  combines two detections from one sensor — so both tracks are updated together almost every
  cycle. A pair that shared updates in more than `duplicate_max_shared_fraction` (25%) of its
  cycles is two objects, and is never retired however close it flies.
- **Each track must be fed on its own.** Over the same cycles, each of the two must have
  received a detection in at least `duplicate_min_turn_fraction` (25%) of them. This is what
  makes the pattern turn-taking rather than starvation. Without it, a track that has simply
  stopped receiving detections — a real object in a sensor dropout, coasting on prediction —
  satisfies "rarely updated together" next to any track that is still fed, and is retired as
  its duplicate. The first version of this rule had exactly that flaw; the ground-truth sweep
  below found it, in the swarm scenario, on tracks five and seven cycles into coasting.
- **The evidence must be sustained and continuous.** `duplicate_min_cycles` (10) cycles in which
  at least one of the pair was updated, all while co-located. A pair that drifts apart starts
  again from nothing, so two real contacts that pass close twice cannot add two short
  coincidences into one long one.
- **The older track keeps its identifier and takes both estimates.** Keeping the older keeps
  the identifier the operator has been watching. But a duplicate can form *because* the older
  track is falling behind its object — the sweep found one in the swarm running 7 m/s slow,
  which lost its drone 2.5 s after the well-fed newcomer was retired. So the survivor does not
  keep its own estimate alone: the two took turns on mostly different measurements, each holds
  roughly half the information, and their information-weighted combination is better than
  either. The change to the survivor is published as an event, so the log still rebuilds into
  what the tracker holds. The retired track ends in LOST like any other, with
  `reason: duplicate`, the survivor and the evidence counts recorded.

  Choosing whichever track had received more updates was tried first and measured worse: over
  ten cycles a lead of one or two is noise, and it swapped the operator's long-standing track
  for a newcomer 87 times across the library. Measured on ground truth, every scenario, two
  cadences — object-cycles in which a true object had no track within 150 m and 8 m/s of it:

  | Survivor policy | Unfollowed | Identity swaps |
  |---|---|---|
  | No duplicate retirement at all | 411 | — |
  | **Older, combined estimate (chosen)** | **413** | **0 of 358** |
  | Older, own estimate | 417 | 0 of 356 |
  | Younger if it had 2.5× the updates | 422 | 22 of 354 |
  | Younger if it had 1.5× the updates | 479 | 87 of 353 |

  By position alone (any track within 150 m) every policy scored identically to no retirement
  at all: none ever left an object without a track near it.
- **An alert on the retired track closes as `duplicate_track`, naming the survivor** — not as
  `track_lost`, which tells an operator something may still be out there unwatched.

## Why this is safe, and how that was checked

The argument above is structural, so it was checked against ground truth rather than trusted:
every scenario in the library, run for its full duration at three cadences (exactly 0.5 s,
0.55 s, and 0.5 s with up to 150 ms of jitter, which is what a live server does), with every
retirement compared against the simulator's true objects. A retirement is wrong if the retired
track and the survivor are nearest different true objects. Those runs include
`h_track_crossing`, built to catch exactly this, and the twenty-object `i_swarm_saturation`.

Result: 541 retirements; none left a true object without a track following it (within 150 m
and 8 m/s), and in none were the retired track and the survivor following different objects.
The first version of the rule — without the per-track turn requirement — failed this sweep with
five such merges, all coasting tracks in the swarm and the crossing.

On the live server, `b_approaching_threat` sampled every 3 s for 150 s: one confirmed track in
48 of 49 samples, against a persistent second track in about 63% of samples before.

Unit tests pin each part: one object on two tracks is reduced to one; two objects 15 m apart,
inside the co-location gate, are never merged; two short meetings do not add up. Each was shown
to fail with its rule removed.

## Consequences

- One drone is one track, after a short delay: the duplicate is visible for the ten cycles of
  evidence (about five seconds at the shipped tick) before it is retired.
- **The limit, stated.** Two real objects that stay closer than sensor noise *and* are each seen
  by only one, different, sensor would present one stream and could be merged. That is an
  information limit — the sensors are not telling them apart — not a tuning choice, and when
  they separate the second object's detections start a new track.
- The situation assessment's `duplicate_tracks_suspected` finding stays, for the window before a
  duplicate is retired and for anything this rule does not catch.
