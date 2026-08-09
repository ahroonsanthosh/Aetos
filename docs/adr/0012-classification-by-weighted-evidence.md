# ADR 0012 — Classification by weighted evidence, and what provenance means

- **Status:** Accepted
- **Date:** 2026-08-09
- **Amends:** [ADR 0002](0002-detection-schema-provenance.md), on the meaning of the
  `provenance` marker. The guard test and the rule that the tracker consumes no inferred
  field are unchanged.

## Context

Classification answers *what an object is*, separately from tracking (*where*) and risk
(*how dangerous*). Until now `backend/classification/` was an empty package and every track
carried the default `unknown` at zero confidence, which the interface honestly reported as
"not assessed" (ADR 0011).

Three things make an operator trust a classification: it must be **defensible** (every
number traceable to a reason), **calibrated** (a thin-evidence track must sound uncertain),
and **honest about disagreement** (a 55/45 split must not be presented as a verdict).

## Decision

### 1. Output a distribution, not a label

`TrackClassification` carries a probability distribution across the full label set, plus the
`label`/`confidence` pair it already had — now derived from the distribution rather than
asserted independently. The top-1 fields remain for consumers that only need a headline.

A single label discards precisely the information an operator needs when the system is
unsure. `small_uas 0.55 / bird 0.40` and `small_uas 0.55 / medium_uas 0.40` have identical
top-1 output and completely different operational meaning.

### 2. Combine independent evidence sources by weighted linear pooling

Each source returns a partial distribution over labels and a weight:

```
P(label) ∝ Σ_source  weight_source × p_source(label)
```

Linear pooling rather than a product of likelihoods (naive Bayes), for two reasons. It is
robust to a source assigning zero to the true label — under a product, one confident,
wrong source vetoes every other, which is exactly the failure mode of a single miscalibrated
sensor. And each source's contribution stays a readable percentage of the result, which is
what makes the output explainable rather than merely computed.

Sources implemented now:

| Source | Evidence | Weight driven by |
|---|---|---|
| `SensorHintEvidence` | `Detection.classification` reported by sensors | Detection quality, hint confidence, agreement across sensors |
| `KinematicEvidence` | Speed, altitude, vertical rate against per-label envelopes | How discriminating the observed values are |

Deliberately **not** implemented yet, with the interface shaped to take it: behavioural
evidence (loiter versus transit). It needs positional history the classifier does not
currently receive, and the risk engine will need the same computation — building it once,
there, is better than building it twice.

### 3. Confidence is not the top probability

Reported `confidence` folds three things: how peaked the distribution is, how much total
evidence weight produced it, and track maturity (detection count and coasting state).

A distribution can be sharp for bad reasons — one detection from one sensor that happens to
be confident. Reporting 0.9 for that would be well-formed and wrong. Multiplying peakedness
by an evidence-strength term means a thin track reads as uncertain no matter how decisive its
single data point looks.

### 4. Envelopes are configuration

Per-label kinematic envelopes live in `config/classification/profiles.json`, not in code —
the same rule sites and sensor registries follow. They are **soft trapezoidal memberships**,
not hard bounds: an object at 26 m/s is not disqualified from a class whose typical band ends
at 25.

These are stated as plausible generic envelopes for a demonstration system, not as
measured characteristics of any real airframe.

## The provenance question

Classification is the first module intended to consume `Detection.classification`, which
forced a question ADR 0002 left implicit: what does `provenance` describe?

The simulated adapter marks every optional field `simulated_truth`, and documents its
reasoning — the marker "records where a value came from, not how plausible it looks". Under
that reading the tag is correct: the number originates in the simulator whatever noise is
applied.

**That reading is changed here, and the reason is ADR 0002's own primary rule.**

A confusion-model draw is not ground truth. It is a deliberately degraded observation —
drawn against `correct_probability`, with the true label *excluded* from the alternatives so
a wrong draw is genuinely wrong. It is the simulator modelling what a real EO/IR sensor would
report, and a real adapter would mark that identical quantity `measured`.

If the simulator marks it differently, then `provenance` becomes a channel that distinguishes
simulated from real input. Any downstream module that discounts by provenance — which is
exactly what ADR 0002 instructs classification to do — would behave differently in
SIMULATION and LIVE mode **on identical data**. That is the failure ADR 0002 exists to
prevent, arriving through a field intended to help.

So:

- **`provenance` describes how a value was produced**, not which process produced it.
  A noise-degraded measurement is `measured`; a value computed from a sensor's own
  measurements is `derived`; `simulated_truth` is reserved for a value copied from ground
  truth without degradation.
- Simulated-versus-real is carried by `SensorRef.sensor_type` (`simulated_eo_ir` versus
  `eo_ir`) and its `is_simulated` property, which is the correct channel and already exists.
- The simulated adapter consequently emits **no** `simulated_truth` at all. That is a
  statement about the simulator's honesty, not a gap: it degrades everything it emits.

Unchanged: the tracker still consumes no inferred field, and the guard test still strips
`kinematics` and `classification` wholesale and requires identical track output. Provenance
was never what made that guarantee hold.

## Consequences

- Classification is testable against deliberately conflicting evidence, because sensor
  disagreement is now representable rather than uniformly discounted.
- Adding a learned classifier later means adding an evidence source, or replacing the
  combiner behind the same interface. The module boundary does not move.
- `CLASSIFICATION_CHANGED` becomes a projected event carrying a full track snapshot, so a
  classification is reconstructible from the log like every other track state (ADR 0006).
- An operator can be shown a distribution that is *mostly* one label without that being
  reported as certainty. Whether the interface surfaces disagreement well is a design
  question this ADR does not settle; it only guarantees the data is there to surface.

## Amendment — evidence memory and state ownership

Added after running the module against a live scenario. Two things were wrong in ways no
unit test could show, and both are architectural rather than tuning.

### Evidence accumulates over a track's life

The first version classified each cycle from that cycle's hints alone. A track under
continuous, consistent observation was therefore no better classified than a brand new one,
the answer flapped between labels (`bird`, `other`, `unknown`) on evidence that never
changed, and a single coasting cycle erased everything the system had learned.

`ClassificationService` now holds a **bounded** memory of each track's recent sensor hints
and classifies from all of them. Bounded rather than a running tally so it can still change
its mind: if association hands a track to the wrong object, an unbounded count would keep the
old identity alive indefinitely.

The consequence worth stating plainly: a gap in sensor reporting is not evidence that the
earlier reports were wrong, and the display must not behave as though it were.

### Classification produces the value; tracking owns it

The first version applied its result to a copy of the track. The tracker, which owns track
state and emits a full snapshot on every track event, knew nothing about it — so the next
`TRACK_UPDATED` reverted every track to `unknown` and the API served `unknown` for
confidently labelled tracks (FM-025).

`ClassificationService` now writes back through a narrow `ClassificationSink` protocol,
implemented by `TrackManager`. The dependency points from classification to a *capability*,
not to the tracker, so tracking remains replaceable; and the tracker holds the value opaquely,
reading no meaning from it, so ADR 0002's separation is untouched.

**The general rule this establishes:** exactly one component owns each piece of track state,
and every other module hands it a value rather than keeping a copy. That is what makes an
event's snapshot the whole truth and therefore what makes the log replayable (ADR 0006). Risk
scoring must use the same path when it is built.
