# ADR 0017 — Sensor capability declaration, and a compatibility floor that is a test result

**Status:** Accepted
**Date:** 2026-08-22

## Context

Aetos claims to be hardware-agnostic: a customer connects the radar, cameras, RF sensors and
ADS-B receiver they already own rather than replacing them. The architecture supports that
claim — `SensorAdapter` and the canonical `Detection` are a genuine narrow waist, and
`sensors/adapters/README.md` has always said a new source is an addition to one directory.

But "hardware-agnostic" as stated is unfalsifiable, and it collapses on the second question a
customer asks: *then what exactly does my radar have to do?* Any answer given in prose has three
problems.

1. **Every vendor will claim to meet it.** A requirements list in a PDF is a thing to be agreed
   with in a sales meeting and discovered to be false on site.
2. **It has no failure semantics.** A sensor that is worse than the list allows is not thereby
   useless — a fixed camera with poor localisation is still worth having — but a pass/fail list
   has no way to say so, so the honest answer becomes "it depends", which is not an answer.
3. **The dangerous case is invisible to it.** The failure that actually damages the system is
   not a poor sensor. It is a sensor that *understates its own error*. The filter uses reported
   uncertainty as the Kalman gain, so a source claiming 2 m and delivering 15 m will pull tracks
   off real objects, capture neighbours' detections through an over-tight gate, and swap two
   identities with high confidence. A requirements list phrased as "position accuracy better
   than X" actively rewards this: it makes optimistic declaration the way to pass.

The third point is the one that determines the shape of the decision. Aetos propagates
uncertainty faithfully at every stage rather than discarding or replacing it, which means the
picture is exactly as good as its inputs — no better, and no worse. A source that reports its
error honestly is usable at almost any accuracy, because everything downstream sizes itself
accordingly. A source that lies is unusable at any accuracy.

## Decision

### 1. The floor is honesty, not accuracy

The entry requirement is that a source makes a specific, checkable claim about itself and
behaves consistently with it. Accuracy determines *which floor* it reaches, not *whether* it is
admitted.

Stated as the specification states it: Aetos does not require a good sensor. It requires a
sensor that tells the truth about how good it is.

### 2. Three ordinal floors and one independent grade

`INGESTIBLE` (0) → `TRACKABLE` (1) → `FUSABLE` (2), cumulative, plus `CLASSIFYING` as a grade
orthogonal to all three.

Ordinal rather than pass/fail because the failure modes are genuinely graded: a source may be
unable to sustain a track alone and still contribute usefully to one another sensor is holding.
Collapsing that into a boolean would either exclude useful hardware or admit hardware whose
limitations then surface as unexplained tracking behaviour.

Classification is a grade rather than a rung because identity evidence and position quality are
orthogonal in practice — a camera with a vision model may localise badly and classify well, and
a ranging radar the reverse.

### 3. Every adapter declares `SensorCapabilities`, separately from `SensorHealth`

A static declaration of what the source *is*, reported alongside the existing health record,
which reports what it is *doing*.

Kept separate deliberately. Folding capability into health would make a declared specification
appear to change when a cable came loose, and would make the document unavailable before the
sensor is connected — which is exactly when a customer wants to see it.

### 4. The floors are computed from tracking's own constants, not written down

`sensors/adapters/capabilities.py` derives each floor's thresholds at runtime from
`TrackManagerConfig`, `AssociationConfig` and the deployment's pipeline tick, rather than
carrying hard-coded numbers that agree with them by inspection.

This is the difference between a specification and a duplicate. Retuning the association gate or
the tick interval changes what a sensor must do to be trackable, and a number written into a
second file would silently stop describing the system it claims to describe.

### 5. Conformance is a runnable suite, shipped with the product

`sensors/adapters/conformance/` is a package under `sensors/`, not a module under `tests/`. It
takes any `SensorAdapter`, exercises it, and reports the floor reached with every failure named.

Under `sensors/` because a third party integrating their own hardware must be able to run it
against their own adapter without a copy of our test suite. "Aetos-compatible" then means a
report with a date on it rather than an assertion in a meeting.

### 6. The suite reports *declared* conformance, and says so

No test can establish that a sensor claiming 3 m accuracy delivers it. That requires measurement
against a known reference and is an integration activity.

The suite therefore checks structural conformance, lifecycle discipline, coverage adherence,
declaration self-consistency and whether the declared figures clear the computed floors — and
labels its own output as declared conformance in those words.

## Rationale

Point 5 is the part that actually works; the rest is design that supports it. A specification
document decays the moment the code moves, and a requirements list agreed in prose has no
mechanism at all. A suite a customer's engineer can run against their own hardware, which
recomputes its thresholds from the tracker's live constants, is the only version of this that
stays true without anyone maintaining it.

Point 6 is what stops the mechanism from becoming the thing it was built to prevent. A
conformance report that claimed to verify accuracy would be exactly the over-claim this record
exists to eliminate, arriving one level up.

## Consequences

- `SensorAdapter` gains a fifth abstract method. Every adapter must declare itself, including
  the simulated one — which is correct: the simulator is subject to the same contract, and its
  declaration is what lets the conformance suite have a first subject before any hardware exists.
- Registry documents gain a discriminated `kind`. Sensor characteristics were previously
  simulation-only parameters, and a live driver's document is a different shape.
- Sales and integration conversations acquire a concrete artefact. This is a deliberate outcome
  rather than a side effect.
- Retuning tracking now has a second visible consequence: it can move a sensor between floors.
  The conformance suite makes that visible at the moment of the change rather than in the field.

## Alternatives considered

- **A prose requirements list in the README.** What most of this industry does. Rejected for the
  three reasons in Context; it is unfalsifiable and rewards optimistic declaration.
- **A single pass/fail certification.** Simpler to explain. Rejected: it either excludes hardware
  that is genuinely useful in a supporting role, or admits hardware whose limits then surface as
  unexplained behaviour with no declared cause.
- **Hard-coded floor thresholds in the specification document.** Rejected under §4 — a number in
  a second place is a number that will stop agreeing with the first.
- **Inferring capability by observing an adapter rather than having it declare.** Attractive
  because observation cannot lie. Rejected: coverage, latency and timestamp accuracy are not
  observable from the detection stream alone, and a source observed over a quiet hour would be
  measured as having no capability at all.
