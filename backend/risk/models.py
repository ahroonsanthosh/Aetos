"""What a risk assessment is: a score, a level, and the factors that produced both.

The types here are the module's contract with the rest of the system. They are deliberately
richer than a number and a band, because a bare score is a black-box output an operator can
neither act on nor challenge (spec §10, ADR 0013).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from backend.tracking.models import RiskLevel

RISK_SCHEMA_VERSION = "1.0.0"


class FactorContribution(BaseModel):
    """One factor's opinion about one track, and what it did to the score.

    `activation` is the factor's own reading in [0, 1]; `weight` is how much the policy lets
    that reading matter. The two are kept apart so a reason code can say *what was observed*
    without the configured weighting being baked into it — retuning a weight must not silently
    rewrite the sentence an operator reads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    """Stable machine identifier, e.g. `approaching_protected_zone`. Safe to alert on, filter
    by, and compare across versions in a way a prose sentence is not."""

    weight: float = Field(ge=0.0, le=1.0)
    activation: float = Field(ge=0.0, le=1.0)

    contribution: float = Field(default=0.0, ge=0.0, le=1.0)
    """How much this factor actually moved the combined escalation — its marginal effect, not
    `weight × activation`. With noisy-OR, a factor arriving alongside strong others adds less
    than the same factor arriving alone, and the ordering an operator reads should reflect
    what mattered rather than what was configured."""

    detail: str
    """One human sentence, stating what was observed rather than what was computed. "Closing
    at 24 m/s, 1.4 km from the protected area" is useful; "closure activation 0.68" is an
    implementation detail wearing a sentence."""

    @property
    def material(self) -> bool:
        """Whether this factor said anything worth showing an operator."""
        return self.activation > 0.0 and self.weight > 0.0


class RiskAssessment(BaseModel):
    """The complete finding for one track at one moment.

    Score and confidence are separate and neither is derived from the other: how bad it looks
    and how much the system believes its own assessment are different questions, and
    multiplying them would produce a number that answers neither (ADR 0013).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = RISK_SCHEMA_VERSION

    score: int = Field(default=0, ge=0, le=100)
    """Raw, unsmoothed, this cycle. ADR 0007 applies hysteresis to the *level* only, so the
    number an operator sees is the one the engine actually computed."""

    level: RiskLevel = RiskLevel.NONE
    """After hysteresis. May lag the score deliberately — see ADR 0007."""

    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    """How much the assessment should be believed, from track support, freshness, coasting and
    whether classification has run. Never multiplied into the score."""

    mitigation: float = Field(default=1.0, ge=0.0, le=1.0)
    """The discount applied for positive evidence of harmlessness. 1.0 means none was applied,
    which is the case for every unknown contact — absence of evidence never reduces risk."""

    factors: list[FactorContribution] = Field(default_factory=list)
    """Every factor consulted, including those that found nothing. Keeping the silent ones is
    what lets an operator see that a concern was *checked* rather than merely absent."""

    assessed: bool = False
    """Whether risk assessment actually ran against this track.

    A score of zero from a module that ran means "we looked, and nothing here is concerning".
    A score of zero from one that did not means nothing at all. Those must never render the
    same way (ADR 0011)."""

    @property
    def reasons(self) -> list[str]:
        """Human-readable reason codes, most influential first.

        Ordered by marginal contribution rather than activation, so the list reads in the
        order the factors actually moved the score.
        """
        material = [factor for factor in self.factors if factor.material]
        material.sort(key=lambda factor: factor.contribution, reverse=True)
        return [factor.detail for factor in material]

    @property
    def codes(self) -> list[str]:
        """The machine-readable equivalent of `reasons`, in the same order."""
        material = [factor for factor in self.factors if factor.material]
        material.sort(key=lambda factor: factor.contribution, reverse=True)
        return [factor.code for factor in material]
