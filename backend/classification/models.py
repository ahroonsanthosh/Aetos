"""Classification outputs: a distribution, and the evidence that produced it.

The distribution is the primary result. `label` and `confidence` are derived from it rather
than asserted alongside it, so the headline can never disagree with the detail behind it.

See docs/adr/0012-classification-by-weighted-evidence.md.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sensors.schemas.detection import ClassificationLabel

CLASSIFICATION_SCHEMA_VERSION = "1.0.0"


class EvidenceContribution(BaseModel):
    """One source's opinion, and how much it should count.

    Retained on the result rather than discarded after combining, because a classification an
    operator cannot interrogate is a classification they have to take on trust — and the whole
    argument for a rule-based classifier over a learned one at this stage is that it can be
    interrogated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    """Which evidence source produced this, e.g. `sensor_hint` or `kinematic`."""

    weight: float = Field(ge=0.0)
    """How much this source counted, relative to the others. Zero means it had nothing to
    say — a source with no data abstains rather than voting for uniformity, which would
    otherwise drag every result towards the flat distribution."""

    distribution: dict[ClassificationLabel, float]
    """This source's opinion alone, normalised. Empty when the source abstained."""

    detail: str
    """Human-readable justification. Shown to operators, so it states what was observed
    rather than naming the mechanism that observed it."""


class ClassificationResult(BaseModel):
    """What the classifier concluded about one track."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = CLASSIFICATION_SCHEMA_VERSION

    distribution: dict[ClassificationLabel, float]
    """Probability across the full label set. Sums to 1.0 whenever any evidence existed."""

    label: ClassificationLabel
    """The most probable label. Ties resolve to `unknown` rather than to whichever label
    happens to sort first — an arbitrary winner presented as a finding is worse than an
    honest admission that nothing separated them."""

    confidence: float = Field(ge=0.0, le=1.0)
    """Not the top probability. Folds how peaked the distribution is, how much evidence
    produced it, and how mature the track is — a sharp distribution built on one detection
    from one sensor is well-formed and not yet worth believing (ADR 0012)."""

    evidence: list[EvidenceContribution] = Field(default_factory=list)

    @property
    def assessed(self) -> bool:
        """Whether any source actually contributed.

        False means the classifier ran and had nothing to work with, which is different from
        it not having run at all. Both are honestly reported as unassessed by the interface,
        but only this one means the pipeline is healthy and the evidence is simply absent.
        """
        return any(contribution.weight > 0.0 for contribution in self.evidence)

    @field_validator("distribution")
    @classmethod
    def _validate_distribution(
        cls, value: dict[ClassificationLabel, float]
    ) -> dict[ClassificationLabel, float]:
        if not value:
            return value

        total = sum(value.values())
        # Tolerance rather than exact equality: the distribution is assembled by float
        # arithmetic across several sources, and demanding an exact 1.0 would reject correct
        # results for reasons that have nothing to do with classification.
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"distribution must sum to 1.0, got {total}")
        if any(probability < 0.0 for probability in value.values()):
            raise ValueError("distribution probabilities must be non-negative")
        return value


def top_label(distribution: dict[ClassificationLabel, float]) -> ClassificationLabel:
    """The most probable label, resolving ties and emptiness to `unknown`.

    A tie means the evidence did not separate two hypotheses. Picking one anyway would
    present a coin toss as a conclusion.
    """
    if not distribution:
        return ClassificationLabel.UNKNOWN

    best = max(distribution.values())
    winners = [label for label, probability in distribution.items() if probability >= best - 1e-9]
    if len(winners) != 1:
        return ClassificationLabel.UNKNOWN
    return winners[0]
