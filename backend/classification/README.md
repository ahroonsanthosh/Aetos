# Classification

**What a track is, how confident the system is in that, and why.**

Tracking answers *where*. This module answers *what*, and it is deliberately a separate
module so either can be replaced without touching the other. Nothing here estimates a
position; nothing in tracking reads a label.

## What it produces

A distribution over the label set, not a single answer:

```
small_uas   0.75
other       0.11
unknown_uas 0.07
unknown     0.06
bird        0.01
```

with a top label, a confidence figure, and one plain-language reason per contributing
evidence source.

The distribution is the output and the label is derived from it, so the headline can never
disagree with the detail. A bare top-1 answer discards exactly what an operator needs when
the system is unsure: `small_uas 0.55 / bird 0.40` and `small_uas 0.55 / medium_uas 0.40`
have the same winner and completely different operational meaning.

**Confidence is not the top probability.** It is the product of three independent things,
because any one of them being weak should pull the answer down:

- *peakedness* — the leader's margin over its nearest rival, not its raw share
- *evidence strength* — how much weight produced it, saturating so weight alone cannot drive
  confidence to 1
- *maturity* — how many detections the track rests on, discounted while it coasts

So a sharp-looking distribution built from one detection reads as uncertain, which is the
correct thing to show.

## How evidence combines

Each `EvidenceSource` forms an opinion from one kind of information and knows nothing about
the others. `Classifier` pools them by **weighted linear** combination — not a product of
likelihoods, so one confident and wrong source cannot veto every other by assigning a label
zero probability (ADR 0012).

Two sources exist today:

| Source | Reads | Notes |
|---|---|---|
| `SensorHintEvidence` | Sensor-reported classifications | Usually the strongest signal — a sensor that classifies is using imagery or RF signature that never reaches this module in raw form. Weighted by the sensor's stated confidence, detection quality, and provenance. |
| `KinematicEvidence` | Speed, altitude, vertical rate against per-label envelopes | Weak on its own by design; earns its place by ruling things *out*. A 400 m altitude excludes a ground vehicle whatever a sensor claims. Abstains while a track coasts, because predicted motion is not an observation. |

Adding a source — radar cross-section, behavioural, a learned model — means implementing
`EvidenceSource` and registering it in `build_default_classifier`. Nothing already here
changes.

**Abstention is not a shrug.** A source with nothing to say returns weight zero, never a
uniform distribution. A uniform vote is not neutral: pooled with real evidence it drags the
result flat, so a source that knows nothing would actively erase what other sources know.

## Evidence accumulates over a track's life

`ClassificationService` remembers each track's recent sensor hints (`_HINT_MEMORY`, bounded)
and classifies from all of them, not from the current cycle alone.

This is not an optimisation. Judging each cycle in isolation made the answer flap — `bird`,
then `other`, then `unknown` — on a track that was one object throughout, and made a single
coasting cycle erase everything already learned about it. A gap in sensor reporting is not
evidence that the earlier reports were wrong.

The memory is bounded rather than a running tally so it can still change its mind. If
association ever hands a track to the wrong object, an unbounded count would keep the old
identity alive indefinitely.

## Who owns the result

Classification computes the value; **the tracker stores it**.

`ClassificationService` writes each result back through a `ClassificationSink` — implemented
by `TrackManager` — rather than keeping its own copy of track state. One owner per piece of
state is what makes an event's track snapshot the whole truth, and therefore what makes the
log replayable (ADR 0006).

This was learned the hard way. The first version returned classified *copies* and recorded
them in the log. The next cycle's `TRACK_UPDATED`, emitted by the tracker from a snapshot
with no classification in it, silently reverted every track to `unknown`. Every unit test
passed and the event counts looked healthy; the API served `unknown` for tracks the
classifier had confidently labelled. `tests/unit/test_classification_service.py` pins the
behaviour by asserting on what a consumer reads, not on what was written.

## Configuration

Kinematic envelopes live in `config/classification/profiles.json` — deployment-specific
judgements, and burying them in code makes them invisible to the people best placed to
correct them. Each profile carries soft trapezoidal bands (full membership across the typical
range, falling linearly to zero at the absolute bounds) and a prior that stops a rare class
winning on a marginal envelope match alone.

Profile membership is the geometric mean across dimensions, so a single hard disagreement is
decisive: an arithmetic mean would let a ground vehicle at 400 m survive on a plausible
speed.

## Known limits

- **One sensor's hint per track per cycle** (FM-024). The association gives each track one
  detection per cycle, so hints from several sensors observing the same object are never
  combined within a cycle — they arrive as separate tracks. Fusion fixes this; the memory
  above mitigates it across time but does not solve it.
- **Rule-based.** Every number traces to a source and a stated reason, which is what lets an
  operator disagree with it usefully. A learned classifier slots in behind the same interface
  without the module boundary moving.

## Files

| File | Purpose |
|---|---|
| `models.py` | `ClassificationResult`, `EvidenceContribution`, `top_label` |
| `evidence.py` | `EvidenceSource` and the two implementations; `Band`, `KinematicProfile` |
| `classifier.py` | Pooling, confidence calibration, profile loading |
| `service.py` | Per-cycle orchestration, evidence memory, write-back, event emission |

See [ADR 0012](../../docs/adr/0012-classification-by-weighted-evidence.md).
