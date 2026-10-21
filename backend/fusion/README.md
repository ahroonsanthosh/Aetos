# Fusion

**Several sensors' observations of one object, made one.**

Without this stage, three sensors watching one drone produce three tracks. Each sensor emits a
detection every cycle; global nearest neighbour assigns at most one detection per track; the
surplus detections are unassigned; unassigned detections start new tracks. Every component
behaves exactly as designed and the operator is shown three contacts where there is one
(FM-023, observed live before it was theorised).

## Where it sits, and why not in association

```
ingestion → FUSION → tracking → classification → risk → decision
```

Before tracking, on detections. The question here is "are these two the same object", which is
different from association's "which track does this detection belong to".

Loosening the association gate until it absorbed the surplus would have been the smaller change
and is the wrong one. A gate wide enough to take three detections of one object is wide enough
to take a detection belonging to a *neighbouring* object, which is an identity swap — two real
contacts merging into one. Scenario H exists to catch precisely that. The trade is not
symmetrical:

> **An over-count is visible. An under-count is not.**

An operator shown three tracks for one drone can see something is wrong. An operator shown one
track where there are two has been told the sky is emptier than it is, and nothing on the
display contradicts it. Every default in this module leans towards the visible error.

## What decides that two detections are one object

The **statistical gate**: squared Mahalanobis distance between the two positions under their
combined covariance, below a chi-squared threshold. The threshold is **the tracker's own** — the
pipeline passes `TrackManager.association_gate_threshold` in — so fusion cannot merge a pair the
tracker would have insisted on separating. A separately configured copy would drift, and tracks
would flicker between fused and split.

Two structural rules sit beside it: two detections from one sensor are never fused, and **a real
detection is never fused with a simulated one** (HYBRID mode; a sensor nothing attached claims
is fused with nothing).

Three things constrain it:

- **`max_fusable_uncertainty_m` (400 m)** — the bound that does the real work. The gate scales
  with reported uncertainty, so a sensor claiming an enormous covariance produces a gate wide
  enough to swallow separate contacts: fusion at its most aggressive exactly where the
  measurements are least trustworthy. This constrains the *measurement* instead. A detection
  too vague to place within 400 m is not evidence about which object it is.

  A flat distance ceiling was tried first and is the wrong instrument. To catch a pathological
  covariance it must sit below the legitimate spread of good sensors — and this site's radars
  report σ ≈ 45–75 m, so two true observations of one object are routinely 200–300 m apart. Set
  tight enough to be useful, it split the very tracks fusion exists to join. It survives as
  `max_pair_separation_m` (750 m), set generously, to bound the arithmetic rather than decide.

- **`require_distinct_sensors`** — two detections from one sensor are never fused. A sensor
  reports each object once per scan, so two returns from one sensor in one cycle are two
  objects, or a duplicate that ingestion already removed by `detection_id` (FM-013). Fusing
  them would mean concluding one sensor saw one object twice, which the input cannot mean. This
  also bounds every cluster at one detection per sensor, which is what makes single-linkage
  grouping safe against chaining.

- **Ambiguity rejection** — a detection admissible to *more than one* cluster is fused into
  none of them. There is no evidence in one cycle of positions for choosing between two
  candidate objects, and choosing anyway would present a guess to the operator as a
  reconciliation.

## How the measurement is combined

Inverse-covariance weighting: information adds, and the fused mean is the information-weighted
average. A confident sensor pulls the result towards its reading in proportion to how confident
it actually is, rather than every sensor getting an equal vote — an equal-vote average would let
the worst sensor on the site drag the position of every object it can see.

The fused covariance is smaller than any input's, which is the point. Three independent looks at
one object genuinely do locate it better than one. That claim is only sound because the inputs
are independent measurements of the same thing, which is what the clustering establishes and
what the same-sensor rule stops a duplicate from violating.

## Classification hints (FM-024)

A fused measurement carries a synthesised identifier (`fused:det-a+det-b`), which is **not** a
key in ingestion's hint map — those are keyed by original detection ids. Routing hints by the
assignment alone would therefore find nothing for any fused track, and every multi-sensor object
would sit at `unknown` forever.

`FusedDetection.contributing_detection_ids` maps back to the parts, and
`CycleEvidence.from_fused_assignments` uses it, so a track observed by several classifying
sensors is identified on all of their opinions. Being better observed must not make a track
*less* confidently classified.

## Measured effect

| Scenario | Truth | Confirmed tracks, fusion off | Fusion on |
|---|---|---|---|
| `b_approaching_threat` | 1 object | 3 | **1** |
| `h_track_crossing` | 2 objects | 6 | **4** |

## What this does not fix

**Two objects closer than the sensor noise may transiently merge.** In `h_track_crossing` the
contacts pass within ~60 m while the radars report σ ≈ 45–75 m. Two objects separated by less
than one standard deviation of the measurement noise are not distinguishable *by position
alone*. That is an information limit, not a tuning problem, and no gate setting clears it.

What separates crossing contacts is velocity — which is why the tracker holds filter state and
association uses it (ADR 0005). Fusion sees one cycle of positions and has no such evidence, so
where the two overlap it declines to choose and otherwise may merge. Measured at ~1.6% of
multi-sensor groups across a full crossing run, concentrated entirely in the final approach;
`tests/integration/test_fusion_pipeline.py` bounds it so a regression that makes merging routine
fails the suite.

**`h_track_crossing` still over-counts** (4 tracks for 2 objects). Fusion reduces fragmentation;
it does not eliminate it, and fragmentation across *time* (FM-022, a track lost and re-acquired
under a new id) is a different failure this module does not address.

**Fusion is stateless across cycles.** It reconciles within one cycle and remembers nothing.
Two tracks that were wrongly separated stay separate until the detections themselves fall inside
the gate. A stateful version could carry an object hypothesis forward, at the cost of being able
to carry a *wrong* one forward too.

## What fusion leaves, and what catches it

When two radars' detections of one object land just outside the gate and a broad sensor gates
with both, the ambiguity rule fuses nothing and the surplus detection starts a second track. The
two tracks then take turns winning the single fused measurement, so neither goes stale. That is
caught one stage later, in the tracker (ADR 0021, FM-023): two tracks that are co-located,
co-moving and taking turns for ten cycles are one object, and the younger is retired into the
older. It is not done here because fusion sees one cycle of positions; telling one object fed
twice from two objects flying together needs the update history only the tracker has.
