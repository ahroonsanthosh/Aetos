# Risk

**How dangerous is this, why, and how much should you believe it.**

Tracking answers *where*. Classification answers *what*. This answers *how dangerous*, and
`backend/decision` answers *what should a human consider doing*. Four questions, four modules,
so any of them can be replaced without touching the others.

## The safety property

**Absence of evidence never reduces risk.**

Only a positive, confident identification of something harmless may lower a score, and never
to zero. An unknown contact behaving like a threat scores like a threat — a system that scored
it low because it did not know what it was would be quietest exactly when it should be
loudest. `tests/unit/test_risk.py` tests this from five directions.

## What it produces

```
RISK  HIGH · 71                          100% confidence

BECAUSE
  • On course to enter the protected area in 2 min.
  • Tracked continuously for 87 s across 128 detections.
  • Classified medium uas at 38% confidence.
  • Closing on the site at 30 m/s.
  • Heading within 4° of a direct course to the site.

▸ 4 further checks, nothing found
```

Every score arrives with the factors that produced it, ordered by how much each actually moved
it — and with the factors that were checked and found nothing, because "did it consider
loitering?" deserves an answer.

**Score and confidence are separate, and confidence is never multiplied into the score.** A
thin-evidence track on alarming geometry is exactly the case an operator must see; folding the
two together would quietly suppress it.

## The factors

| Factor | Group | Reads |
|---|---|---|
| `zone_incursion` | engagement | Occupied zone, weighted by `ZoneKind.severity` |
| `closing_on_protected_area` | engagement | Time-to-boundary on the current course |
| `proximity_to_protected_area` | engagement | Distance, log-scaled, heading-independent |
| `closing_velocity` | engagement | Radial speed toward the asset |
| `direct_approach` | engagement | Angle between course and bearing to the asset |
| `loitering_near_asset` | behaviour | Path length versus net displacement, near the asset |
| `low_altitude_near_asset` | behaviour | Altitude, gated on proximity |
| `identified_as_uas` | corroborating | Classification mass on UAS labels × confidence |
| `persistent_track` | corroborating | Detection count and track age |

Adding a concern — RF emission, correlated approach, airspace deconfliction — is one class
implementing `EscalationFactor` plus one config entry. Nothing existing changes.

## How they combine

Not one rule but three, because the factors are three different kinds of statement:

- **Engagement: weighted maximum.** These are five views of one situation, not five
  independent concerns — a contact that is close is usually also closing, on a direct course,
  and short on time. The strongest statement the geometry makes sets the base.
- **Behaviour: noisy-OR with engagement.** Loitering and low-altitude approach are genuinely
  independent of the trajectory picture, so they add.
- **Corroborating: scales the base, cannot create it.**
  `base + (1 − base) · base · corroboration`. At zero base it does exactly nothing.

Then `score = 100 · escalation · mitigation`.

This is the second version. The first applied plain noisy-OR to all nine and was wrong twice
over — a track 11 km away scored 25 for having been observed two minutes, and an ordinary
inbound contact at 700 m pinned at 100 with nowhere left to go. Both are recorded in ADR 0013's
amendment; both were found by running the engine, not by reading it.

## Level, not just score

`RiskLevel` comes from the score through the hysteresis in ADR 0007: dual rise/fall thresholds
plus an asymmetric minimum dwell, escalation fast and de-escalation slow. The **score is never
smoothed** — an operator looking at the number sees what the engine computed this cycle, so it
can never disagree with the reasons attached to it.

The failure this prevents is not a wrong level. It is forty alerts for one object holding
station on a threshold, and the operator desensitisation that follows.

## Who owns the result

Risk computes the value; **the tracker stores it**, through the same `apply_threat` write-back
path classification uses. One owner per piece of track state is what makes an event's snapshot
the whole truth and therefore what makes the log replayable (ADR 0006, FM-025).

## Configuration

`config/risk/policy.json` — factor weights, groups, per-factor thresholds, the mitigation floor,
and the hysteresis bands. Deployment-specific judgements about what a particular site considers
dangerous; burying them in code makes them invisible to the people best placed to correct them,
and unauditable by anyone asked to trust the output.

Validation is strict (`extra="forbid"`), so a typo in a factor name is a startup error rather
than a silently ignored setting.

## Known limits

- **No inter-track reasoning.** Several contacts converging from different bearings is a
  recognised pattern this factor set cannot see, because every factor assesses one track alone.
  It needs a cycle-level view and is not built.
- **Constant-velocity geometry.** Time-to-boundary assumes the current course is held. It is
  labelled as a projection everywhere it surfaces.
- **Rule-based.** Every number traces to a factor and a sentence, which is what lets an
  operator disagree with it usefully.

## Files

| File | Purpose |
|---|---|
| `models.py` | `RiskAssessment`, `FactorContribution` |
| `factors.py` | `EscalationFactor` and the nine implementations; mitigation |
| `engine.py` | Combination, confidence, policy loading |
| `hysteresis.py` | Score → level, ADR 0007 |
| `service.py` | Per-cycle orchestration, loiter memory, write-back, `RISK_CHANGED` events |

See [ADR 0013](../../docs/adr/0013-risk-by-escalating-factors.md) and
[ADR 0007](../../docs/adr/0007-risk-hysteresis.md).
