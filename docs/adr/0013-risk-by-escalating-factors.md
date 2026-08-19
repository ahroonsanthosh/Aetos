# ADR 0013 — Risk as combined escalating factors, discounted only by positive evidence of harmlessness

- **Status:** Accepted
- **Date:** 2026-08-09
- **Builds on:** [ADR 0007](0007-risk-hysteresis.md) (level hysteresis, unchanged),
  [ADR 0012](0012-classification-by-weighted-evidence.md) (classification as a distribution)

## Context

Risk answers *how dangerous is this*, separately from tracking (*where*), classification
(*what*) and decision (*what should a human consider doing*). Until now `backend/risk/` was an
empty package and every track carried a zero score, which the interface honestly reported as
"not assessed" (ADR 0011).

The requirement is a score an operator can act on **and argue with**. That rules out a single
opaque number, and it rules out a model whose inputs cannot be inspected. It also has to be
driven by many inputs at once — proximity, closure, geometry, identity, behaviour, persistence
— because no single one of them means much alone. A drone hovering 200 m outside the fence and
a bird crossing it at the same range are not the same situation, and neither is a fast inbound
contact at 4 km versus one transiting past at the same speed and distance.

Three combination rules were considered.

**Weighted sum.** Simple and standard, and wrong here in a specific way: eight mildly elevated
factors sum to the same score as one severe one. "Slightly unusual in eight respects" is not a
critical threat, but a sum cannot tell the difference.

**Maximum factor.** Fixes that, and throws away the corroboration that makes a picture
convincing. A contact that is close *and* closing *and* classified as a UAS *and* has loitered
is more concerning than one that merely does the worst of those, and a max reports them
identically.

**Noisy-OR** — `1 − Π(1 − wᵢ·aᵢ)`. Chosen, with a correction described in the amendment
below. Each factor is an opportunity to raise the score; a single strong factor drives it up on
its own, corroborating factors add real but diminishing amounts, and the result saturates
rather than exploding. It has a plain-language reading — *the chance that at least one of these
concerns is real* — which is what makes the reason codes honest rather than a post-hoc gloss on
arithmetic.

Noisy-OR assumes the factors are **independent**. That assumption does not hold uniformly here,
and the amendment is what fixes it.

## Decision

### Escalation combines; only positive evidence of harmlessness discounts

```
escalation = combine(factors)          see "Amendment — factor groups" for the rule
score      = 100 · escalation · mitigation
```

`mitigation` is 1.0 — no discount — unless classification has **positively and confidently**
identified the object as something benign. Absence of evidence never reduces risk.

This is the safety property of the whole module, and it is deliberately asymmetric:

- An **unknown** contact behaving like a threat scores like a threat. An unassessed track is
  not a safe track, and a system that scored it low because it "doesn't know what it is" would
  be quietest exactly when it should be loudest.
- A **confidently classified bird** is discounted, but never to zero, and the discount is
  bounded (`min_mitigation` in configuration). Classification can be wrong, and a bird
  penetrating a protected zone is still a fact worth an operator's attention.

### The factors

Grouped by the question they answer, all configured rather than hard-coded.

| Factor | Reads | Why it earns its weight |
|---|---|---|
| `zone_incursion` | Occupied zone and its kind | The strongest single statement the geometry can make. Weighted by `ZoneKind.severity`. |
| `time_to_protected` | Time-to-boundary on current course | The sharpest operational number there is — "ninety seconds out" is actionable in a way "1.8 km" is not. |
| `proximity` | Distance to the protected area | Heading-independent. A contact holding station just outside the fence is not closing, and is still a problem. |
| `closure` | Radial speed toward the asset | Distinguishes inbound from transiting at identical range. |
| `approach_directness` | Angle between velocity and bearing to asset | A course that points straight at the asset is different from one that merely has an inbound component. |
| `loiter` | Path length versus net displacement, near the asset | Persistent presence with little net travel. Surveillance behaviour looks like this and nothing else in the factor set catches it. |
| `low_altitude_penetration` | Altitude, gated on proximity | Low and close is terrain-following behaviour, not overflight. Gated so a low contact 8 km away scores nothing. |
| `identity_threat` | Classification mass on UAS labels × confidence | Uplift only. A confident UAS identification raises risk; an unknown one does not lower it. |
| `persistence` | Detection count and track age | Stops a single noisy cycle reaching a high band. Corroboration, not concern in itself. |

Every factor is an `EscalationFactor` with a stable code, a weight, an activation in [0, 1],
and a human sentence. Adding one means implementing the interface and adding a config entry;
nothing already there changes.

### Score and confidence are separate numbers

As with classification (ADR 0012), how bad it looks and how much the system believes its own
assessment are different questions. A coasting track with three detections and no
classification can legitimately produce a high score from geometry alone; reporting that at
full confidence would overstate the evidence. `confidence` folds track support, freshness,
coasting and whether classification has run.

**Confidence never modulates the score.** Multiplying them would produce a number that is
neither, and would quietly suppress exactly the thin-evidence-but-alarming-geometry case that
most needs an operator's eyes.

### Reason codes are ranked by marginal contribution

A factor's reason is emitted when its activation is material, and the list is ordered by how
much that factor actually moved the score — its marginal contribution to the combined
escalation, not its raw activation or its configured weight. Under every one of the three
combination rules those differ: an engagement factor that is not the strongest contributes
nothing, and a corroborating factor contributes only in proportion to concern others
established. An operator reading top-down gets the factors in the order they mattered.

### Hysteresis is unchanged

ADR 0007 governs the score-to-level mapping in full: dual rise/fall thresholds, asymmetric
minimum dwell, raw unsmoothed score. This ADR supplies the score it operates on.

### Recommendation lives in `backend/decision`

Risk stops at *how dangerous*. Mapping a level to `MONITOR` / `VERIFY` / `ALERT` / `ESCALATE`
/ `RESPONSE_RECOMMENDED` is a policy question about what to put in front of a human, and it
belongs to a module that can be changed without touching the scoring. Keeping them apart is
also what keeps the strongest output in the system — a recommendation that a human *consider*
a response — in one small, auditable place.

Nothing in either module acts. Aetos ends at a sentence recommending that a human consider
something; there is no activation path anywhere in the repository, and no code that causes
anything to happen in the physical world.

## Consequences

- Risk is stateful per track (hysteresis and loiter memory both need history). That state is
  written back through the same sink classification uses, so it rides in the track snapshot on
  every event and replays identically (ADR 0006, FM-025).
- The factor set is the module's public surface. Retuning is a config change; adding a concern
  is one class and one config entry.
- A track can be `assessed` with a score of zero — "we looked, and nothing about this is
  concerning" — which is a genuinely different statement from "not assessed", and the
  interface must keep showing the difference (ADR 0011).
- Because unknown is not discounted, an unclassified contact on a bad trajectory will raise
  alerts. That is intended. The alternative fails silently.

## Amendment — factor groups

Added after running the engine against real geometry. Applying one combination rule to all nine
factors was wrong in two directions at once, and both were visible immediately.

**Correlated factors were counted repeatedly.** Proximity, time-to-boundary, closure, approach
directness and zone incursion are five views of one situation, not five independent concerns: a
contact that is close is usually also closing, on a direct course, and short on time. Noisy-OR
multiplied them as though each were fresh evidence, so an ordinary inbound contact at 700 m
pinned the score at **100** — leaving nowhere to go for one actually inside the protected zone,
and collapsing the entire top half of the scale.

**Corroboration created risk it should only have supported.** A track eleven kilometres away,
travelling away from the site, scored **25** for having been observed for two minutes.
Persistence is a statement about how much track there is, not about danger, and identity is the
same: a confidently identified UAS 20 km out on no approach is not a threat.

Factors therefore declare a group, and each group combines differently:

| Group | Rule | Why |
|---|---|---|
| `engagement` | Weighted **maximum** | Correlated views of one geometric situation. Take the strongest statement the geometry makes and stop. |
| `behaviour` | **Noisy-OR**, with engagement | Loitering and low-altitude approach are genuinely independent of the trajectory picture. |
| `corroborating` | **Scales** the base, cannot create it | `base + (1 − base) · base · corroboration`. At zero base it contributes exactly nothing. |

The group is configuration, not code, so re-classifying a factor as a new one is added is a
config change.

This is a real narrowing of the original decision, and it is worth stating why the first version
looked right: noisy-OR *is* the correct rule for independent evidence, and the factor list reads
like independent evidence until it is run. Nothing in the reasoning was wrong except the
unexamined assumption that all nine factors were the same kind of statement.

## Amendment — curve shape is part of the decision, and must be tested as such

Found by reading the engine against its own documentation rather than by running it, which is
why it survived so long.

`closing_on_protected_area` is the sharpest number the system produces: "ninety seconds out" is
actionable in a way "1.8 km" is not, because it already folds in speed and geometry. Its
documented shape was explicit — *"the difference between 300 s and 240 s barely matters and the
difference between 60 s and 20 s matters enormously"* — and it shipped raising the linear ramp
to the power **0.5** rather than **2**, which is precisely that curve inverted.

The consequence was not subtle. Under the inverted exponent a contact four minutes out on a
straight-line course activated at **0.49**; the intended figure is **0.06**. Because engagement
takes the group *maximum* and this factor carries the second-highest weight in the group, that
one factor routinely set the entire score, and ordinary distant inbound traffic sat a few points
below MEDIUM on geometry that said only "inbound, eventually" — the same over-sensitivity the
first amendment was written to remove, reintroduced through a different door.

Every test passed throughout. The endpoints were correct — full activation at the urgent
threshold, silence beyond the horizon — and only the middle of the curve was wrong. Endpoint
assertions cannot see that.

Two things follow, and the second matters more than the first.

1. The exponent is now `sharpness`, a factor parameter defaulting to 2.0, so the curve is
   tunable per site and its shape is stated in configuration rather than implied by code.
2. **A factor's curve shape is part of this decision, not an implementation detail of it.**
   `tests/unit/test_risk_calibration.py` therefore asserts monotonicity and mid-curve
   relationships across every ramp, not just their endpoints — expressed as relationships
   ("this is below that") rather than exact scores, so a legitimate retune does not fail the
   suite and train people to update expected values without reading them.

## Amendment — corroboration counts sensors, not detections

`multi_sensor_corroboration` was added to the `corroborating` group. The evidence was already
being carried to the operator on every published Track and simply never passed to assessment.

The reasoning is that failure modes must be *independent* to corroborate. A single sensor's
picture can be an artefact of that sensor — radar multipath off a structure, an RF bearing
ghost, a thermal reflection — and no amount of persistence on that one sensor rules it out,
because the error repeats with the observation. A second sensor of a different kind failing the
same way in the same place at the same time is a far less likely coincidence.

It counts *distinct sensors* rather than detections deliberately. A hundred returns from one
radar is persistence, which `persistent_track` already scores; two returns from two sensors is
corroboration. Conflating them would let one chatty sensor impersonate agreement, which is the
opposite of what the factor is for.

Being corroborating, it cannot create concern where the geometry found none: a contact held by
every sensor on site, parked far away going nowhere, still scores zero.
