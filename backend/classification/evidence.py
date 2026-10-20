"""Evidence sources: independent opinions about what a track is.

Each source answers one question from one kind of information, returns a partial distribution
over labels and a weight, and knows nothing about the others. The combiner in `classifier.py`
does the pooling. Adding a source — behavioural, radar cross-section, a learned model — means
implementing `EvidenceSource` and registering it, with no change to anything already here.

Two rules every source follows:

**Abstain rather than shrug.** A source with nothing to say returns weight zero, not a
uniform distribution. A uniform vote is not neutral: pooled with real evidence it drags the
result towards flat, so a source that knows nothing would actively erase what other sources
know.

**Report what was observed, not what was computed.** The `detail` string reaches an operator.
"Speed 31 m/s and altitude 280 m are typical of a medium UAS" is useful; "kinematic envelope
membership 0.82" is an implementation detail wearing a sentence.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from backend.classification.models import EvidenceContribution
from sensors.schemas.detection import ClassificationHint, ClassificationLabel, Provenance


@dataclass(frozen=True, slots=True)
class TrackEvidence:
    """Everything the classifier knows about one track this cycle.

    Assembled by the caller. Deliberately not the `Track` model itself: classification needs a
    narrow, explicit input, and passing the whole track would let a source quietly start
    depending on risk score or lifecycle state — neither of which is evidence about what an
    object *is*.
    """

    track_id: str
    speed_mps: float

    altitude_m: float
    """Height above the **site reference plane**, in metres — not the ellipsoidal altitude the
    `Track` publishes.

    The profiles in `config/classification/profiles.json` are site-relative and say so plainly:
    the `vehicle` band runs from -30 m to +40 m, which is only meaningful as height above the
    ground the vehicle is standing on. Feeding this the ellipsoidal figure added the site's own
    elevation to every track, so the same drone hovering thirty metres up classified as
    `small_uas` at a sea-level site and `unknown_uas` at one a kilometre up — and at a few
    hundred metres it leaned `bird`, which then *discounted* its risk score (FM-0211)."""

    vertical_rate_mps: float
    detection_count: int
    coasting: bool
    hints: tuple[ClassificationHint, ...] = ()
    """Sensor-reported classifications associated with this track this cycle. Empty is
    normal — most sensors do not classify, and a radar profile reports none at all."""

    hint_quality: tuple[float, ...] = ()
    """Per-hint detection quality confidence, positionally matched to `hints`."""


class EvidenceSource(ABC):
    """One way of forming an opinion about what a track is."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier, recorded on the contribution so a result can be traced."""

    @abstractmethod
    def assess(self, evidence: TrackEvidence) -> EvidenceContribution:
        """Form an opinion, or abstain with weight zero."""


# --------------------------------------------------------------------------------------
# Sensor-reported classification
# --------------------------------------------------------------------------------------


class SensorHintEvidence(EvidenceSource):
    """What the sensors themselves said this track is.

    Usually the strongest single signal, because a sensor that classifies at all is doing it
    with information — imagery, RF signature — that never reaches this module in raw form.

    Each hint is weighted by the sensor's own stated confidence in the classification, by the
    detection's quality confidence, and by provenance. Under ADR 0012 a noise-degraded
    measurement is `measured`, so a well-modelled sensor is not discounted; `simulated_truth`
    is discounted almost to nothing, because a value copied from ground truth without
    degradation is exactly what must never be allowed to influence an operational answer.
    """

    _PROVENANCE_WEIGHT = {
        Provenance.MEASURED: 1.0,
        Provenance.DERIVED: 0.8,
        # Not zero: a source is still permitted to say it, and silently ignoring input is
        # worse than counting it for almost nothing and being able to show that it was.
        Provenance.SIMULATED_TRUTH: 0.05,
    }

    @property
    def name(self) -> str:
        return "sensor_hint"

    def assess(self, evidence: TrackEvidence) -> EvidenceContribution:
        if not evidence.hints:
            return _abstain(self.name, "No sensor reported a classification for this track.")

        pooled: dict[ClassificationLabel, float] = {}
        total_weight = 0.0

        for index, hint in enumerate(evidence.hints):
            quality = evidence.hint_quality[index] if index < len(evidence.hint_quality) else 1.0
            weight = hint.confidence * quality * self._PROVENANCE_WEIGHT.get(hint.provenance, 0.5)
            if weight <= 0.0:
                continue
            pooled[hint.label] = pooled.get(hint.label, 0.0) + weight
            total_weight += weight

        if total_weight <= 0.0:
            return _abstain(self.name, "Sensor classifications carried no usable confidence.")

        distribution = {label: value / total_weight for label, value in pooled.items()}

        return EvidenceContribution(
            source=self.name,
            weight=total_weight,
            distribution=distribution,
            detail=self._describe(evidence, distribution),
        )

    def _describe(
        self,
        evidence: TrackEvidence,
        distribution: dict[ClassificationLabel, float],
    ) -> str:
        count = len(evidence.hints)
        sensors = "1 sensor report" if count == 1 else f"{count} sensor reports"

        if len(distribution) == 1:
            only = next(iter(distribution))
            return f"{sensors}, all indicating {_readable(only)}."

        ranked = sorted(distribution.items(), key=lambda item: item[1], reverse=True)
        leader, leader_share = ranked[0]
        runner, runner_share = ranked[1]
        return (
            f"{sensors} disagree: {_readable(leader)} {leader_share:.0%} "
            f"against {_readable(runner)} {runner_share:.0%}."
        )


# --------------------------------------------------------------------------------------
# Kinematic envelopes
# --------------------------------------------------------------------------------------


class Band(BaseModel):
    """A soft trapezoidal membership band.

    Full membership between `typical_min` and `typical_max`, falling linearly to zero at the
    absolute bounds. Hard bounds would make an object at 26 m/s categorically not a class
    whose typical band ends at 25, which is not how any of these categories actually work.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    absolute_min: float
    typical_min: float
    typical_max: float
    absolute_max: float

    def membership(self, value: float) -> float:
        if value <= self.absolute_min or value >= self.absolute_max:
            return 0.0
        if self.typical_min <= value <= self.typical_max:
            return 1.0
        if value < self.typical_min:
            span = self.typical_min - self.absolute_min
            return (value - self.absolute_min) / span if span > 0 else 1.0
        span = self.absolute_max - self.typical_max
        return (self.absolute_max - value) / span if span > 0 else 1.0


class KinematicProfile(BaseModel):
    """One label's expected motion envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: ClassificationLabel
    speed_mps: Band
    altitude_m: Band
    vertical_rate_mps: Band
    prior: float = Field(gt=0.0, le=1.0)
    """Relative plausibility before observing anything. Keeps a rare class from winning on a
    marginal envelope match alone."""

    def membership(self, evidence: TrackEvidence) -> float:
        """Geometric mean across dimensions, so a single hard disagreement is decisive.

        An arithmetic mean would let a class survive an impossible altitude on the strength of
        a plausible speed — a ground vehicle at 400 m would still score respectably. The
        geometric mean sends any zero to zero, which is the correct behaviour for a dimension
        that genuinely rules a class out.
        """
        memberships = (
            self.speed_mps.membership(evidence.speed_mps),
            self.altitude_m.membership(evidence.altitude_m),
            self.vertical_rate_mps.membership(evidence.vertical_rate_mps),
        )
        if any(m <= 0.0 for m in memberships):
            return 0.0
        return math.exp(sum(math.log(m) for m in memberships) / len(memberships))


class KinematicEvidence(EvidenceSource):
    """How the track is moving, against per-label envelopes from configuration.

    Weak evidence on its own and deliberately weighted as such: many of these envelopes
    overlap heavily, and a 20 m/s object at 100 m is genuinely consistent with several
    categories. It earns its place by ruling things *out* — a 400 m altitude excludes a
    ground vehicle no matter what any sensor claims.
    """

    def __init__(self, profiles: tuple[KinematicProfile, ...], weight: float = 0.6) -> None:
        self._profiles = profiles
        self._weight = weight

    @property
    def name(self) -> str:
        return "kinematic"

    def assess(self, evidence: TrackEvidence) -> EvidenceContribution:
        # A coasting track's kinematics are the filter's prediction, not an observation.
        # Classifying on predicted motion would let a track's classification drift while
        # nothing is actually being seen.
        if evidence.coasting:
            return _abstain(
                self.name,
                "Track is coasting; motion is predicted rather than observed.",
            )

        scored = {
            profile.label: profile.membership(evidence) * profile.prior
            for profile in self._profiles
        }
        total = sum(scored.values())

        if total <= 0.0:
            return _abstain(
                self.name,
                f"Motion ({evidence.speed_mps:.0f} m/s at {evidence.altitude_m:.0f} m) "
                "matches no configured profile.",
            )

        distribution = {label: value / total for label, value in scored.items() if value > 0.0}

        return EvidenceContribution(
            source=self.name,
            weight=self._weight,
            distribution=distribution,
            detail=(
                f"Speed {evidence.speed_mps:.0f} m/s at {evidence.altitude_m:.0f} m "
                f"is consistent with {_readable(max(distribution, key=lambda k: distribution[k]))}."
            ),
        )


def _abstain(source: str, detail: str) -> EvidenceContribution:
    return EvidenceContribution(source=source, weight=0.0, distribution={}, detail=detail)


def _readable(label: ClassificationLabel) -> str:
    return str(label).replace("_", " ")
