"""The risk engine: combines escalating factors into one defensible score.

Not one combination rule but three, because the factors are three different kinds of statement.
Engagement factors take a weighted maximum, behaviour factors noisy-OR with them, and
corroborating factors scale the result without being able to create it. `_combine_readings`
carries the full reasoning, and ADR 0013's amendment records what the single-rule version got
wrong when it met real geometry.

Only positive evidence of harmlessness lowers a score, and it is applied once, here, rather
than by any factor. Absence of evidence never reduces risk — that is the module's safety
posture, and the property its tests are built around.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from backend.common.config import ConfigError, ConfigPaths
from backend.risk.correlation import SituationPolicy
from backend.risk.factors import (
    ApproachDirectnessFactor,
    ClosureFactor,
    EscalationFactor,
    FactorGroup,
    FactorPolicy,
    IdentityThreatFactor,
    LoiterFactor,
    LowAltitudePenetrationFactor,
    PersistenceFactor,
    ProximityFactor,
    TimeToProtectedFactor,
    TrackRisk,
    ZoneIncursionFactor,
    mitigation_for,
)
from backend.risk.hysteresis import HysteresisPolicy
from backend.risk.models import FactorContribution, RiskAssessment
from backend.zones.geometry import SiteGeometry

DEFAULT_POLICY_FILE = "policy.json"

# Detections at which track support stops discounting confidence.
_SUPPORT_SATURATION = 10.0

# Smallest discount worth reporting as a reason code.
#
# The multiplier is always applied to the score — it is part of the arithmetic. What this
# governs is whether the operator is *told* about it. Below this, the discount is invisible in
# a score rounded to a whole number, and the reason line reads "Discounted: classified bird (0%
# of the distribution ...)" — a sentence asserting a judgement the system did not really make,
# quoting a percentage that rounded to nothing. A reason list is only worth reading if every
# line in it changed something.
_MATERIAL_MITIGATION = 0.02

_FACTOR_TYPES: dict[str, type[EscalationFactor]] = {
    "zone_incursion": ZoneIncursionFactor,
    "closing_on_protected_area": TimeToProtectedFactor,
    "proximity_to_protected_area": ProximityFactor,
    "closing_velocity": ClosureFactor,
    "direct_approach": ApproachDirectnessFactor,
    "loitering_near_asset": LoiterFactor,
    "low_altitude_near_asset": LowAltitudePenetrationFactor,
    "identified_as_uas": IdentityThreatFactor,
    "persistent_track": PersistenceFactor,
}


class RiskPolicy(BaseModel):
    """The complete tuning surface: factor weights, mitigation floor, level thresholds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    notes: str = ""
    """Free text for whoever maintains the file. Declared rather than tolerated as an unknown
    key, because `extra="forbid"` is what turns a typo in a factor name into a startup error
    instead of a silently ignored setting — and a config format that quietly drops what it
    does not understand is how a site ends up running tuning nobody applied."""

    factors: dict[str, FactorPolicy]
    hysteresis: HysteresisPolicy
    situation: SituationPolicy = SituationPolicy()
    """Cycle-level assessment. Defaulted rather than required so an existing site's policy
    file keeps loading."""

    min_mitigation: float = Field(default=0.35, ge=0.0, le=1.0)
    """The floor on the harmlessness discount. Never zero: classification can be wrong, and a
    bird penetrating a protected zone is still a fact worth an operator's attention. A discount
    to nothing would delete a track from the picture on the strength of a guess."""


class RiskEngine:
    """Scores one track at a time from a fixed set of factors."""

    def __init__(self, factors: tuple[EscalationFactor, ...], policy: RiskPolicy) -> None:
        self._factors = factors
        self._policy = policy

    @property
    def factors(self) -> tuple[EscalationFactor, ...]:
        return self._factors

    def assess(self, risk: TrackRisk, geometry: SiteGeometry) -> RiskAssessment:
        readings = [(factor, factor.assess(risk, geometry)) for factor in self._factors]
        escalation = _combine_readings(readings)
        mitigation, mitigation_reason = mitigation_for(risk, self._policy.min_mitigation)

        scored = [_with_contribution(reading, readings, _combine_readings) for reading in readings]
        if mitigation_reason is not None and (1.0 - mitigation) >= _MATERIAL_MITIGATION:
            # Recorded as a contribution so it appears in the reason list like everything else.
            # An operator who sees a score *drop* is owed the same explanation as one who sees
            # it rise, and burying the discount would make the number unexplainable.
            #
            # Immaterial discounts are still applied to the score and deliberately not reported:
            # see `_MATERIAL_MITIGATION`.
            scored.append(
                FactorContribution(
                    code="benign_classification",
                    weight=1.0 - mitigation,
                    activation=1.0 - mitigation,
                    contribution=escalation * (1.0 - mitigation),
                    detail=mitigation_reason,
                )
            )

        return RiskAssessment(
            score=round(100.0 * escalation * mitigation),
            confidence=_confidence(risk),
            mitigation=mitigation,
            factors=scored,
            assessed=True,
        )


Reading = tuple[EscalationFactor, FactorContribution]


def _combine_readings(readings: list[Reading]) -> float:
    """Combine factors into one escalation in [0, 1], respecting what kind each one is.

    Three rules, because the factors are three different kinds of statement (ADR 0013,
    amendment — plain noisy-OR across all of them was the first version and was wrong twice).

    **Engagement factors combine by weighted maximum.** Proximity, time-to-boundary, closure,
    approach directness and zone incursion are five views of one situation, not five
    independent concerns: a contact that is close is usually also closing, on a direct course
    and short on time. Noisy-OR treats them as independent and so counts the same fact five
    times — an ordinary inbound contact at 700 m pinned the score at 100 and left nowhere to
    go for one actually inside the protected zone. The maximum takes the strongest statement
    the geometry makes and stops there.

    **Behaviour factors combine by noisy-OR with engagement.** Loitering and low-altitude
    approach genuinely are independent of the trajectory picture, so they add.

    **Corroborating factors scale what is already there, and cannot create it.** Identity and
    persistence multiply the base rather than joining the OR. A confidently identified UAS
    20 km away on no approach is not dangerous, and a track is not dangerous merely for having
    lasted two minutes — which is exactly what the first version scored it at.
    """
    engagement = max(
        (
            contribution.weight * contribution.activation
            for factor, contribution in readings
            if factor.group is FactorGroup.ENGAGEMENT
        ),
        default=0.0,
    )
    behaviour = _noisy_or([c for factor, c in readings if factor.group is FactorGroup.BEHAVIOUR])
    corroboration = _noisy_or(
        [c for factor, c in readings if factor.group is FactorGroup.CORROBORATING]
    )

    base = 1.0 - (1.0 - engagement) * (1.0 - behaviour)

    # Corroboration closes a fraction of the gap to certainty, proportional to how much
    # concern there already is. At base zero it does nothing at all.
    return _clamp(base + (1.0 - base) * base * corroboration)


def _noisy_or(contributions: list[FactorContribution]) -> float:
    """The complement of every factor independently finding nothing."""
    survival = 1.0
    for contribution in contributions:
        survival *= 1.0 - contribution.weight * contribution.activation
    return _clamp(1.0 - survival)


def _with_contribution(
    reading: Reading,
    all_readings: list[Reading],
    combine: Callable[[list[Reading]], float],
) -> FactorContribution:
    """Attach the factor's effect on the combined escalation, for ranking the reason codes.

    The natural definition — full combination minus the combination without this factor — is
    right for behaviour and corroborating factors and badly wrong for engagement ones, in a way
    that only shows up on real data. Engagement factors are near-duplicates by construction, so
    removing the strongest leaves the second-strongest to take its place and the difference is
    small. The strongest factor is the one that *set* the score, and it was being ranked below
    persistence — producing the sentence "this is risky because we have tracked it for 43
    seconds", which is not why it was risky at all.

    So the strongest engagement factor is credited with the whole group's effect, and the ones
    it masked are credited with nothing. That is what actually happened: had any of them been
    absent, the score would have been identical.
    """
    factor, contribution = reading
    if not contribution.material:
        return contribution

    if factor.group is FactorGroup.ENGAGEMENT:
        strength = contribution.weight * contribution.activation
        strongest = max(
            other.weight * other.activation
            for candidate, other in all_readings
            if candidate.group is FactorGroup.ENGAGEMENT
        )
        if strength < strongest:
            return contribution.model_copy(update={"contribution": 0.0})

        without_group = [
            candidate
            for candidate in all_readings
            if candidate[0].group is not FactorGroup.ENGAGEMENT
        ]
        return contribution.model_copy(
            update={"contribution": max(0.0, combine(all_readings) - combine(without_group))}
        )

    without = [candidate for candidate in all_readings if candidate is not reading]
    return contribution.model_copy(
        update={"contribution": max(0.0, combine(all_readings) - combine(without))}
    )


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _confidence(risk: TrackRisk) -> float:
    """How much this assessment should be believed — never how bad it is.

    Deliberately not multiplied into the score (ADR 0013). Doing so would produce a figure that
    is neither the risk nor the certainty, and would quietly suppress the thin-evidence,
    alarming-geometry case that most needs an operator's eyes on it.
    """
    support = min(1.0, risk.detection_count / _SUPPORT_SATURATION)
    freshness = 0.6 if risk.coasting else 1.0

    # Classification is corroboration, not a prerequisite. A track assessed on geometry alone
    # is still a real assessment, so its absence costs a fraction rather than most of the
    # confidence.
    identified = 1.0 if risk.classification_assessed else 0.75

    return round(
        min(1.0, max(0.0, support * freshness * identified * max(0.1, risk.track_confidence))), 4
    )


def load_risk_policy(
    paths: ConfigPaths | None = None, filename: str = DEFAULT_POLICY_FILE
) -> RiskPolicy:
    """Read the risk policy from `config/risk/`.

    Weights, thresholds and dwell times are deployment-specific judgements about what a
    particular site considers dangerous. Burying them in code makes them invisible to the
    people best placed to correct them, and unauditable by anyone asked to trust the output.
    """
    resolved = paths or ConfigPaths.default()
    file = resolved.config_root / "risk" / filename

    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{file}: risk policy not found") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{file}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{file}: could not be read: {exc}") from exc

    try:
        policy = RiskPolicy.model_validate(document)
    except Exception as exc:  # pydantic ValidationError, reported with the file that caused it
        raise ConfigError(f"{file}: invalid risk policy: {exc}") from exc

    unknown = set(policy.factors) - set(_FACTOR_TYPES)
    if unknown:
        raise ConfigError(
            f"{file}: no such risk factor: {', '.join(sorted(unknown))}. "
            f"Known factors: {', '.join(sorted(_FACTOR_TYPES))}"
        )

    return policy


def build_risk_engine(paths: ConfigPaths | None = None) -> RiskEngine:
    """The engine as assembled for normal operation.

    A factor absent from configuration is absent from the engine. That is deliberate: a site
    that does not want loiter detection removes the entry rather than setting its weight to
    zero, so the configuration says what the system is doing rather than what it is not.
    """
    policy = load_risk_policy(paths)
    factors = tuple(
        _FACTOR_TYPES[code](factor_policy) for code, factor_policy in policy.factors.items()
    )
    return RiskEngine(factors, policy)


def policy_path(paths: ConfigPaths | None = None) -> Path:
    """Where the policy is read from. Exposed for diagnostics and tests."""
    resolved = paths or ConfigPaths.default()
    return resolved.config_root / "risk" / DEFAULT_POLICY_FILE
