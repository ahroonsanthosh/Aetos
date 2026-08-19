"""What to put in front of a human.

The last step in the pipeline, and deliberately the smallest. Risk answers *how dangerous*;
this answers *what should an operator consider doing about it*, which is a policy question
about attention and escalation rather than a measurement. Keeping them in separate modules
means the scoring can be retuned without touching what gets escalated, and — more importantly
— it keeps the strongest output the system can produce in one small, auditable place.

**Nothing here acts.** Every value is advice to a person. `RESPONSE_RECOMMENDED` means exactly
what it says: a recommendation that a human *consider* a response, decided and carried out by a
separate system with its own authorisation and safety architecture. Aetos ends at this
sentence — there is no activation path anywhere in the repository, and no code that causes
anything to happen in the physical world. See `response/README.md`.
"""

from __future__ import annotations

from backend.risk.models import RiskAssessment
from backend.tracking.models import Recommendation, RiskLevel

# The baseline mapping. Level is the primary input because it is the one an operator already
# reasons in, and because it has hysteresis applied — a recommendation that flapped would
# defeat the whole point of ADR 0007.
_BY_LEVEL: dict[RiskLevel, Recommendation] = {
    RiskLevel.NONE: Recommendation.MONITOR,
    RiskLevel.LOW: Recommendation.MONITOR,
    RiskLevel.MEDIUM: Recommendation.ALERT,
    RiskLevel.HIGH: Recommendation.ESCALATE,
    RiskLevel.CRITICAL: Recommendation.RESPONSE_RECOMMENDED,
}

# Below this, an assessment is too thinly supported to escalate on by itself.
_THIN_EVIDENCE = 0.35

# The recommendations that spend an operator's attention, and so are the ones thin evidence
# should soften into VERIFY. Defined above its use rather than below it: this is the set that
# decides whether a finding interrupts someone.
_ESCALATING = frozenset(
    {Recommendation.ALERT, Recommendation.ESCALATE, Recommendation.RESPONSE_RECOMMENDED}
)


def recommend(assessment: RiskAssessment) -> Recommendation:
    """The recommendation for an assessment.

    Two adjustments to the plain level mapping, both about evidence rather than severity.

    **Thin evidence becomes VERIFY, not silence.** An alarming score on three detections of a
    coasting track is a real thing to tell an operator about — it is just not yet a thing to
    escalate on. Downgrading it to MONITOR would hide it; leaving it at ESCALATE would spend
    an operator's trust on a track the system barely has. `VERIFY` says the useful thing:
    *look at this and find out*.

    **An unassessed track falls back to MONITOR, and carries its own disclaimer.** MONITOR is
    the floor of this enum; there is no "no recommendation" value, and inventing one here would
    put the burden of the distinction on every consumer of a `Recommendation`. The fact that
    risk has never looked at a track travels with the assessment instead, on
    `RiskAssessment.assessed` — which the API surfaces and the interface keys "Not assessed"
    off, rather than inferring it from a recommendation value (ADR 0011).

    So MONITOR here means "nothing to say about this track", not "assessed and found benign".
    Reading those two as the same thing is the mistake ADR 0011 exists to prevent, and the flag
    is what keeps them apart.
    """
    if not assessment.assessed:
        return Recommendation.MONITOR

    baseline = _BY_LEVEL[assessment.level]

    if assessment.confidence < _THIN_EVIDENCE and baseline in _ESCALATING:
        return Recommendation.VERIFY

    return baseline
