"""The classifier: pools independent evidence into one defensible distribution.

Rule-based and deliberately so at this stage. Every number it produces can be traced to a
source and a stated reason, which is what lets an operator disagree with it usefully. A
learned classifier slots in behind the same interface — as an additional `EvidenceSource`, or
by replacing the combiner — without the module boundary moving (ADR 0012).
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.classification.evidence import (
    EvidenceSource,
    KinematicEvidence,
    KinematicProfile,
    SensorHintEvidence,
    TrackEvidence,
)
from backend.classification.models import (
    ClassificationResult,
    EvidenceContribution,
    top_label,
)
from backend.common.config import ConfigError, ConfigPaths
from sensors.schemas.detection import ClassificationLabel

DEFAULT_PROFILES_FILE = "profiles.json"

# Detections at which track maturity stops discounting confidence. Below this a track is new
# enough that its classification rests on very little, however decisive that little looks.
_MATURITY_SATURATION = 8


class Classifier:
    """Combines evidence sources into a classification for one track at a time."""

    def __init__(self, sources: tuple[EvidenceSource, ...]) -> None:
        self._sources = sources

    @property
    def sources(self) -> tuple[EvidenceSource, ...]:
        return self._sources

    def classify(self, evidence: TrackEvidence) -> ClassificationResult:
        contributions = tuple(source.assess(evidence) for source in self._sources)
        distribution = self._pool(contributions)

        if not distribution:
            # Every source abstained. Reported as unknown at zero confidence, with the
            # abstention reasons retained so an operator can see *why* nothing was concluded
            # rather than being shown a bare "unknown".
            return ClassificationResult(
                distribution={},
                label=ClassificationLabel.UNKNOWN,
                confidence=0.0,
                evidence=list(contributions),
            )

        return ClassificationResult(
            distribution=distribution,
            label=top_label(distribution),
            confidence=self._confidence(distribution, contributions, evidence),
            evidence=list(contributions),
        )

    def _pool(
        self, contributions: tuple[EvidenceContribution, ...]
    ) -> dict[ClassificationLabel, float]:
        """Weighted linear pooling: P(label) is proportional to the weighted sum of opinions.

        Linear rather than a product of likelihoods, so one confident and wrong source cannot
        veto every other by assigning a label zero — see ADR 0012.
        """
        pooled: dict[ClassificationLabel, float] = {}
        total_weight = 0.0

        for contribution in contributions:
            if contribution.weight <= 0.0 or not contribution.distribution:
                continue
            total_weight += contribution.weight
            for label, probability in contribution.distribution.items():
                pooled[label] = pooled.get(label, 0.0) + contribution.weight * probability

        if total_weight <= 0.0:
            return {}

        normaliser = sum(pooled.values())
        if normaliser <= 0.0:
            return {}

        return {label: value / normaliser for label, value in pooled.items()}

    def _confidence(
        self,
        distribution: dict[ClassificationLabel, float],
        contributions: tuple[EvidenceContribution, ...],
        evidence: TrackEvidence,
    ) -> float:
        """How much this conclusion should be believed — not how probable the top label is.

        Three independent things have to hold before a classification deserves confidence:
        the evidence has to point somewhere in particular (peakedness), there has to be
        enough of it (weight), and the track it describes has to be real enough to be worth
        classifying (maturity). Any one of them being weak should pull the result down, so
        they multiply rather than average.
        """
        ranked = sorted(distribution.values(), reverse=True)
        leader = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else 0.0

        # Margin over the nearest rival, not the raw top probability: leading a two-horse
        # race 0.51 to 0.49 is not the same as leading it 0.51 to 0.05, though both have the
        # same leader.
        peakedness = leader * (1.0 - runner_up)

        total_weight = sum(c.weight for c in contributions if c.weight > 0.0)
        # Saturating rather than linear: past a point, more agreeing evidence stops adding
        # meaningful certainty, and an unbounded term would let weight alone drive confidence
        # towards 1.
        evidence_strength = total_weight / (1.0 + total_weight)

        maturity = min(1.0, evidence.detection_count / _MATURITY_SATURATION)
        if evidence.coasting:
            # Nothing is currently being observed, so whatever was concluded is ageing.
            maturity *= 0.6

        return round(min(1.0, peakedness * evidence_strength * maturity), 4)


def load_kinematic_profiles(
    paths: ConfigPaths | None = None, filename: str = DEFAULT_PROFILES_FILE
) -> tuple[KinematicProfile, ...]:
    """Read per-label kinematic envelopes from `config/classification/`.

    Envelopes are configuration, not constants, for the same reason sites and sensor
    registries are: they are deployment-specific judgements, and burying them in code makes
    them invisible to the people best placed to correct them.
    """
    resolved = paths or ConfigPaths.default()
    file = resolved.config_root / "classification" / filename

    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{file}: classification profiles not found") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{file}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{file}: could not be read: {exc}") from exc

    entries = document.get("profiles")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{file}: expected a non-empty 'profiles' array")

    try:
        return tuple(KinematicProfile.model_validate(entry) for entry in entries)
    except Exception as exc:  # pydantic ValidationError, reported with the file that caused it
        raise ConfigError(f"{file}: invalid classification profile: {exc}") from exc


def build_default_classifier(paths: ConfigPaths | None = None) -> Classifier:
    """The classifier as assembled for normal operation.

    Sensor hints first only for readability; pooling is order-independent by construction.
    """
    return Classifier(
        sources=(
            SensorHintEvidence(),
            KinematicEvidence(load_kinematic_profiles(paths)),
        )
    )


def profiles_path(paths: ConfigPaths | None = None) -> Path:
    """Where profiles are read from. Exposed for diagnostics and tests."""
    resolved = paths or ConfigPaths.default()
    return resolved.config_root / "classification" / DEFAULT_PROFILES_FILE
