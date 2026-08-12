# ADR 0018 — Fusion reconciles detections, not tracks

**Status:** Accepted
**Date:** 2026-08-12
**Relates to:** [ADR 0005](0005-gnn-association.md) (association), [ADR 0002](0002-detection-schema-provenance.md)
(provenance), FM-023, FM-024

## Context

FM-023 was observed, not predicted: running `b_approaching_threat` against the shipped site, one
drone produced three concurrent CONFIRMED tracks.

The cause is that every stage was working correctly. Three sensors see the object, so each cycle
delivers three detections of it. Global nearest neighbour assigns at most one detection per
track — which is right, because two detections of *different* objects must never collapse into
one track. The surplus two are unassigned, and an unassigned detection initiates a track.

Nothing was broken. A stage was missing.

## Decision

### 1. The reconciliation happens before tracking, on detections

The tempting alternative is to widen the association gate until it absorbs the surplus. It is a
much smaller change and it is wrong. A gate wide enough to admit three detections of one object
is wide enough to admit a detection belonging to a neighbouring object, which is an identity
swap — the failure scenario H exists to catch.

That is worse than the problem it would fix, and the asymmetry is the whole argument:

> An over-count is visible to the operator. An under-count is not.

Three tracks for one drone is obviously wrong to anyone looking at the display. One track where
there are two looks exactly like a quiet sky. Every default in `backend/fusion` leans towards
the visible error, including in the cases where that costs accuracy.

### 2. Measurement fusion, not track fusion

The alternative architecture runs a tracker per sensor and merges the resulting tracks. That is
the right choice when sensors are far apart and separately intelligent, each producing its own
confident picture. It is the wrong one here, for three reasons.

The filter sees one better-conditioned observation instead of three competing ones, so the
estimate benefits from all three sensors rather than being the best of them. Exactly one
component continues to hold track state, which is what lets the log rebuild into what the live
system holds (ADR 0006). And merging tracks afterwards would require reconciling three filters'
disagreeing covariances, then deciding which of three identifiers survives — a question with no
good answer, which the operator would experience as tracks changing id.

### 3. The gate is shared with association, and bounded by measurement quality

`gate_confidence` is deliberately the same value `AssociationConfig` uses. Fusion must not merge
a pair the tracker would have insisted on separating, nor separate a pair it would have merged;
if the two thresholds drifted apart, tracks would flicker between fused and split.

The gate scales with reported uncertainty, which creates a perverse incentive: a sensor claiming
an enormous covariance produces a gate wide enough to swallow genuinely separate contacts, so
fusion would be most aggressive exactly where the measurements are least trustworthy. The bound
that fixes this constrains the **measurement** rather than the separation —
`max_fusable_uncertainty_m`. A detection too vague to place within 400 m is not evidence about
which object it is, and is left alone.

A flat distance ceiling was implemented first and removed. To catch a pathological covariance it
has to sit below the legitimate spread of good sensors, and this site's radars report σ ≈ 45–75 m
— so two true observations of one object are routinely 200–300 m apart. At 250 m it split the
very tracks fusion existed to join, turning the FM-023 fix from 3→1 into 3→2. It survives at
750 m as an arithmetic backstop, not as the decision.

### 4. Two structural rules: distinct sensors, and ambiguity rejection

**A sensor's two returns are never fused.** A sensor reports each object once per scan, so two
returns from one sensor in one cycle are two objects — or a duplicate, and ingestion already
removed those by `detection_id` (FM-013). Fusing them would mean concluding one sensor saw one
object twice, which the input structurally cannot mean. This also bounds every cluster at one
detection per sensor, which is what makes single-linkage grouping safe against chaining.

**A detection admissible to more than one cluster is fused into none.** One cycle of positions
contains no evidence for choosing between two candidate objects, and choosing anyway would
present a guess as a reconciliation. This is what makes a crossing safe: as two contacts
converge their detections begin gating against both clusters, and fusion backs off to leaving
them alone — an over-count for a few cycles rather than a merge.

### 5. Contributions are carried, so hints survive

A fused measurement carries a synthesised identifier, which is not a key in ingestion's hint
map. Routing hints by the assignment alone would find nothing for any fused track, so every
multi-sensor object would sit at `unknown` permanently — a worse outcome than the FM-024
dilution it was meant to address. `FusedDetection.contributing_detection_ids` maps back to the
parts and `CycleEvidence.from_fused_assignments` consumes it.

Fusion produces a `FusedDetection`, converted to `IncomingDetection` for the tracker rather than
widening that type. `IncomingDetection` is what makes the tracker structurally incapable of
reading fields ADR 0002 forbids, and adding provenance to it would weaken that guarantee to buy
nothing: tracking has no use for provenance, and the pipeline routes it to classification
directly.

## Consequences

**Good.** FM-023's observed case closes: `b_approaching_threat` goes from three CONFIRMED tracks
to one. `h_track_crossing` improves from six to four. Classification receives every contributing
sensor's opinion (FM-024). A single-sensor site is bit-identical with fusion on or off.

**Costs, and they are real.** Two objects closer than the sensor noise may transiently merge —
in `h_track_crossing` the contacts pass within ~60 m while σ ≈ 45–75 m, and two objects
separated by less than one standard deviation of the measurement noise are not distinguishable
by position alone. Measured at ~1.6% of multi-sensor groups, concentrated entirely in the final
approach, and bounded by a test. This is an information limit rather than a tuning problem: what
separates crossing contacts is velocity, and fusion has one cycle of positions.

**Not addressed.** Fusion is stateless across cycles, so it carries no object hypothesis forward
— and cannot carry a wrong one forward either. Track fragmentation across *time* (FM-022) is a
different failure and is untouched. `h_track_crossing` still over-counts.

**Consequently `backend/risk/correlation.py`'s clustering stays.** FM-026 notes that its
track-clustering exists as an interim measure and should be deleted "when `backend/fusion`
exists". It is kept, because fusion does not eliminate the over-count it reports — it reduces
it. Deleting the reporting while the condition can still occur would remove the operator's only
signal that the display may be showing more tracks than there are objects.
