"""The individual concerns that make up a risk score.

Each factor answers exactly one question from one kind of information, returns an activation
in [0, 1] and a sentence saying what it observed, and knows nothing about the others. The
engine in `engine.py` combines them. Adding a concern — RF emission, correlated approach,
airspace deconfliction — means implementing `EscalationFactor` and adding a config entry, with
no change to anything already here.

Two rules every factor follows.

**Report what was observed, not what was computed.** The `detail` string reaches an operator.
"Closing at 24 m/s, 1.4 km from the protected area" is useful; "closure activation 0.68" is an
implementation detail wearing a sentence.

**Silence is not zero risk, and it is not hidden either.** A factor with nothing to say returns
activation zero *and still returns a contribution*, so an operator can see that the concern was
checked. What a factor must never do is lower the score — only positively identified
harmlessness does that, and it is applied once by the engine rather than smuggled in here
(ADR 0013).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.risk.models import FactorContribution
from backend.zones.geometry import EnuPoint, EnuVelocity, ProjectedZone, SiteGeometry
from sensors.schemas.detection import ClassificationLabel

# Labels that represent an uncrewed aircraft. Everything else is either a natural object or a
# category this system does not treat as an air threat.
UAS_LABELS = frozenset(
    {
        ClassificationLabel.SMALL_UAS,
        ClassificationLabel.MEDIUM_UAS,
        ClassificationLabel.LARGE_UAS,
        ClassificationLabel.UNKNOWN_UAS,
    }
)

# Labels that positively identify something as *not* an air threat. Only these can earn a
# mitigation discount — `unknown` is deliberately absent, and that absence is the safety
# property of the whole module.
BENIGN_LABELS = frozenset({ClassificationLabel.BIRD, ClassificationLabel.VEHICLE})


@dataclass(slots=True)
class TrackRisk:
    """Everything the risk engine knows about one track this cycle.

    Assembled by the caller. Deliberately not the `Track` model itself: risk needs a narrow,
    explicit input, and passing the whole track would let a factor quietly start depending on
    the previous risk level — which is how a score becomes self-reinforcing and stops being
    evidence of anything.
    """

    track_id: str
    position: EnuPoint
    velocity: EnuVelocity

    speed_mps: float

    altitude_m: float
    """Height above the **site reference plane**, in metres — the same datum as
    `position.up_m`, and the same one the zone floors and ceilings are converted into.

    Not the ellipsoidal altitude the `Track` publishes. It used to be exactly that, and the
    mismatch silently disabled a threat factor: `low_altitude_near_asset` compares this against
    thresholds of 120 m and 30 m, which plainly mean height above the site, so at any site more
    than 120 m above the ellipsoid the comparison could never succeed. A drone at 40 m over a
    site 500 m up read as "540 m; not a low-altitude approach". The shipped site is at 5 m,
    which is why it looked right.

    One vertical quantity in this module, on one datum. `backend/zones` already worked this way
    — it converts the site document's ellipsoidal bounds at the boundary rather than carrying
    both — and this now matches it (FM-0210)."""

    detection_count: int
    track_age_s: float
    coasting: bool
    track_confidence: float

    classification: dict[ClassificationLabel, float] = field(default_factory=dict)
    """The classification distribution, empty when nothing has classified this track."""

    classification_confidence: float = 0.0
    classification_assessed: bool = False

    path_length_m: float = 0.0
    """Distance actually travelled over the remembered window."""

    net_displacement_m: float = 0.0
    """Straight-line distance between the ends of that window. The ratio of the two is what
    separates loitering from transiting."""

    loiter_window_s: float = 0.0
    """How much history the two figures above cover. Below a useful minimum the loiter factor
    abstains rather than calling a two-second sample a hover."""


class FactorGroup(StrEnum):
    """How a factor combines with the others.

    The distinction exists because the factors are not all the same *kind* of statement, and
    combining them as though they were is what made the first version of this engine both
    over-sensitive and saturated (ADR 0013, amendment).
    """

    ENGAGEMENT = "engagement"
    """Geometry and trajectory: where it is, where it is going, how fast. These are five views
    of one situation and move together — a contact that is close is usually also closing, on a
    direct course, and short on time-to-boundary. Treating them as independent concerns counts
    the same fact five times."""

    BEHAVIOUR = "behaviour"
    """What it is doing that geometry alone does not capture. Genuinely independent of
    engagement: an object can loiter far out or dash in high."""

    CORROBORATING = "corroborating"
    """Identity and track support. These make an existing concern more credible; they are not
    concerns in themselves. A confidently identified UAS 20 km away on no approach is not
    dangerous, and neither is a track merely because it has lasted two minutes."""


class FactorPolicy(BaseModel):
    """Per-factor tuning. Configuration, not constants, because these are deployment-specific
    judgements and burying them in code hides them from the people best placed to correct
    them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    weight: float = Field(ge=0.0, le=1.0)
    """The most this factor may contribute on its own, before combination."""

    group: FactorGroup = FactorGroup.ENGAGEMENT
    """Which combination rule applies. See `FactorGroup`."""

    enabled: bool = True

    parameters: dict[str, float] = Field(default_factory=dict)
    """Factor-specific thresholds. Read by name, with a documented default, so adding a
    parameter does not invalidate every existing site's configuration."""

    @field_validator("parameters")
    @classmethod
    def _reject_unusable_parameters(cls, values: dict[str, float]) -> dict[str, float]:
        """Every parameter must be a finite, non-negative number.

        This was the one configuration surface in the system with no validation at all, and
        both ways it could be wrong were silent:

        - A **negative** value inverts the judgement rather than failing. `fast_mps: -25`
          makes a target closing at 20 m/s score zero — a threat factor switched off by a
          typo, with nothing anywhere saying so.
        - A **zero** divisor raised `ZeroDivisionError` from inside the assessment, killing
          the risk stage and with it the whole pipeline cycle, so the operator's picture
          stopped advancing for a reason that appeared only in a log.

        Zero itself stays legal: several parameters are floors where zero honestly means "no
        minimum". Divisor safety is handled at the point of division, where three of the five
        sites already guarded and two did not.

        Rejecting at load matches every other configuration surface here — a bad value is an
        error when the file is read, not a surprise on the cycle that first touches it.
        """
        for name, value in values.items():
            if not math.isfinite(value):
                raise ValueError(f"parameter {name!r} must be a finite number, got {value!r}")
            if value < 0.0:
                raise ValueError(
                    f"parameter {name!r} must not be negative, got {value!r}. A negative "
                    "threshold does not disable a factor, it inverts it — silently."
                )
        return values

    def parameter(self, name: str, default: float) -> float:
        return self.parameters.get(name, default)


class EscalationFactor(ABC):
    """One reason a track might be more concerning than the ground state."""

    SUPPORTED_PARAMETERS: frozenset[str] = frozenset()
    """Every parameter name this factor reads.

    Declared so that a name it does *not* read can be rejected when the policy loads. Without
    it, `parameters` accepted anything and every unrecognised key fell through to the default:
    tuning `fast_mp` instead of `fast_mps` left the factor on its default of 25 m/s while the
    file said 100, and nothing anywhere reported it. The policy read as configured and the
    system behaved as though it were not.

    Every other configuration surface here rejects unknown fields — `extra="forbid"` on the
    models, unknown factor codes refused by `build_risk_engine`. A free-form dict cannot use
    `extra="forbid"`, so it declares its vocabulary instead.

    `tests/unit/test_risk.py` derives these sets from the source and requires them to match
    exactly, so a parameter added to a factor without being declared here fails the build
    rather than becoming unconfigurable.
    """

    def __init__(self, policy: FactorPolicy) -> None:
        self._policy = policy

    @property
    @abstractmethod
    def code(self) -> str:
        """Stable machine identifier, recorded on the contribution so a score can be traced."""

    @property
    def weight(self) -> float:
        return self._policy.weight if self._policy.enabled else 0.0

    @property
    def group(self) -> FactorGroup:
        return self._policy.group

    @abstractmethod
    def assess(self, risk: TrackRisk, geometry: SiteGeometry) -> FactorContribution:
        """Form an opinion, or return `self.silent(...)` with the reason nothing was found."""

    def contribution(self, activation: float, detail: str) -> FactorContribution:
        return FactorContribution(
            code=self.code,
            weight=self.weight,
            activation=_clamp(activation),
            detail=detail,
        )

    def silent(self, detail: str) -> FactorContribution:
        return FactorContribution(code=self.code, weight=self.weight, activation=0.0, detail=detail)


# --------------------------------------------------------------------------------------
# Geometry — where it is and where it is going
# --------------------------------------------------------------------------------------


class ZoneIncursionFactor(EscalationFactor):
    """The object is inside a configured zone right now.

    The strongest single statement the geometry can make, and the only factor that does not
    have to infer anything: it is either in there or it is not. Weighted by `ZoneKind.severity`
    so presence in monitored airspace — which is expected and unremarkable — does not read like
    presence over the asset.
    """

    @property
    def code(self) -> str:
        return "zone_incursion"

    def assess(self, risk: TrackRisk, geometry: SiteGeometry) -> FactorContribution:
        zone = geometry.most_consequential_zone(risk.position)
        if zone is None:
            return self.silent("Not inside any configured zone.")

        activation = self._severity_activation(zone)
        if activation <= 0.0:
            return self.silent(f"Inside {zone.name}, which is monitored airspace only.")

        return self.contribution(
            activation,
            f"Inside {zone.name} ({zone.kind.value} zone) at {risk.altitude_m:.0f} m.",
        )

    def _severity_activation(self, zone: ProjectedZone) -> float:
        # Monitored presence is the ground state at a site that bothered to define monitored
        # airspace, so it activates nothing on its own; restricted and protected escalate.
        return {0: 0.0, 1: 0.6, 2: 1.0}.get(zone.kind.severity, 0.0)


class TimeToProtectedFactor(EscalationFactor):
    """Seconds until the current course enters the protected area.

    The sharpest operational number the system can produce — "ninety seconds out" is actionable
    in a way "1.8 km" is not, because it already folds in speed and geometry. Ramps steeply as
    the time shortens rather than linearly, since the difference between 300 s and 240 s barely
    matters and the difference between 60 s and 20 s matters enormously.
    """

    SUPPORTED_PARAMETERS = frozenset({"horizon_s", "urgent_s"})

    @property
    def code(self) -> str:
        return "closing_on_protected_area"

    def assess(self, risk: TrackRisk, geometry: SiteGeometry) -> FactorContribution:
        horizon_s = self._policy.parameter("horizon_s", 300.0)
        urgent_s = self._policy.parameter("urgent_s", 45.0)

        seconds = geometry.time_to_protected_area_s(risk.position, risk.velocity)
        if seconds is None:
            return self.silent("Current course does not enter the protected area.")
        if seconds > horizon_s:
            return self.silent(
                f"Would reach the protected area in {seconds / 60:.0f} min on this course — "
                "beyond the assessment horizon."
            )

        # Full activation at or inside the urgent threshold, decaying to zero at the horizon.
        if seconds <= urgent_s:
            activation = 1.0
        else:
            activation = 1.0 - (seconds - urgent_s) / max(1e-6, horizon_s - urgent_s)
            # Squared so the curve is steep near the urgent end and shallow near the horizon.
            activation = activation**0.5 if activation > 0 else 0.0

        return self.contribution(
            activation,
            f"On course to enter the protected area in {_duration(seconds)}.",
        )


class ProximityFactor(EscalationFactor):
    """How close it is, regardless of where it is going.

    Separate from closure on purpose. An object holding station just outside the fence is not
    closing at all, and is exactly the case an operator most needs raised — a closure-only
    model would score it at nothing.
    """

    SUPPORTED_PARAMETERS = frozenset({"far_m", "near_m"})

    @property
    def code(self) -> str:
        return "proximity_to_protected_area"

    def assess(self, risk: TrackRisk, geometry: SiteGeometry) -> FactorContribution:
        near_m = self._policy.parameter("near_m", 250.0)
        far_m = self._policy.parameter("far_m", 4_000.0)

        distance = geometry.distance_to_protected_area_m(risk.position)
        if distance <= 0.0:
            return self.contribution(1.0, "Inside the protected area boundary.")
        if distance >= far_m:
            return self.silent(f"{_distance(distance)} from the protected area.")

        # Log-scaled: the operationally meaningful steps are 100 m to 200 m and 1 km to 2 km,
        # not 3.9 km to 3.8 km, and a linear ramp gives those equal weight.
        activation = 1.0 - math.log(max(distance, near_m) / near_m) / math.log(far_m / near_m)

        return self.contribution(activation, f"{_distance(distance)} from the protected area.")


class ClosureFactor(EscalationFactor):
    """How fast the gap to the asset is shrinking.

    Radial speed, not ground speed: a contact crossing at 30 m/s at constant range is not
    closing, and reporting its speed as concerning would be describing motion rather than
    approach.
    """

    SUPPORTED_PARAMETERS = frozenset({"fast_mps", "minimum_mps"})

    @property
    def code(self) -> str:
        return "closing_velocity"

    def assess(self, risk: TrackRisk, geometry: SiteGeometry) -> FactorContribution:
        fast_mps = self._policy.parameter("fast_mps", 25.0)
        minimum_mps = self._policy.parameter("minimum_mps", 1.0)

        closure = closure_rate_mps(risk)
        if closure <= minimum_mps:
            if closure < -minimum_mps:
                return self.silent(f"Opening from the site at {abs(closure):.0f} m/s.")
            return self.silent("Not closing on the site.")

        activation = min(1.0, closure / max(1e-6, fast_mps))
        return self.contribution(activation, f"Closing on the site at {closure:.0f} m/s.")


class ApproachDirectnessFactor(EscalationFactor):
    """Whether the course points *at* the asset or merely has an inbound component.

    Two contacts can close at the same rate, one flying directly in and one on a long diagonal
    that will pass by. The difference is intent-shaped, and it is the kind of thing an operator
    reads instantly off a display and a scalar closure rate throws away.
    """

    SUPPORTED_PARAMETERS = frozenset({"minimum_speed_mps", "spread_deg", "tolerance_deg"})

    @property
    def code(self) -> str:
        return "direct_approach"

    def assess(self, risk: TrackRisk, geometry: SiteGeometry) -> FactorContribution:
        minimum_speed = self._policy.parameter("minimum_speed_mps", 2.0)
        tolerance_deg = self._policy.parameter("tolerance_deg", 25.0)
        spread_deg = self._policy.parameter("spread_deg", 70.0)

        if risk.speed_mps < minimum_speed:
            # Below this the filtered heading is mostly noise, and a nearly stationary object's
            # course swings with small position errors.
            return self.silent("Too slow for its course to mean anything.")

        offset = bearing_offset_deg(risk)
        if offset is None:
            return self.silent("Over the site; no approach bearing to assess.")
        if offset >= spread_deg:
            return self.silent(
                f"Course is {offset:.0f}° off the site; transiting rather than approaching."
            )

        if offset <= tolerance_deg:
            activation = 1.0
        else:
            activation = 1.0 - (offset - tolerance_deg) / max(1e-6, spread_deg - tolerance_deg)

        return self.contribution(
            activation, f"Heading within {offset:.0f}° of a direct course to the site."
        )


# --------------------------------------------------------------------------------------
# Behaviour — what it is doing over time
# --------------------------------------------------------------------------------------


class LoiterFactor(EscalationFactor):
    """Persistent presence with little net travel, near the asset.

    Path length against net displacement. A transiting object covers ground; one holding
    station covers distance without going anywhere, and nothing else in the factor set catches
    that — proximity sees it as merely near, closure as not approaching, directness as not
    pointed anywhere. Surveillance behaviour looks exactly like this.
    """

    SUPPORTED_PARAMETERS = frozenset(
        {"loiter_ratio", "minimum_path_m", "minimum_window_s", "relevant_range_m"}
    )

    @property
    def code(self) -> str:
        return "loitering_near_asset"

    def assess(self, risk: TrackRisk, geometry: SiteGeometry) -> FactorContribution:
        minimum_window_s = self._policy.parameter("minimum_window_s", 30.0)
        minimum_path_m = self._policy.parameter("minimum_path_m", 60.0)
        relevant_range_m = self._policy.parameter("relevant_range_m", 3_000.0)
        loiter_ratio = self._policy.parameter("loiter_ratio", 0.35)

        if risk.loiter_window_s < minimum_window_s:
            return self.silent("Not yet observed long enough to judge loitering.")
        if risk.path_length_m < minimum_path_m:
            return self.silent(
                "Has barely moved; too little travel to distinguish hover from noise."
            )

        distance = geometry.distance_to_protected_area_m(risk.position)
        if distance > relevant_range_m:
            return self.silent(
                f"Any loitering is {_distance(distance)} out and not near the asset."
            )

        # 1.0 for a perfectly closed path, 0.0 for a straight line.
        confinement = 1.0 - min(1.0, risk.net_displacement_m / max(1e-6, risk.path_length_m))
        if confinement < (1.0 - loiter_ratio):
            return self.silent("Travelling through rather than holding station.")

        activation = (confinement - (1.0 - loiter_ratio)) / max(1e-6, loiter_ratio)
        return self.contribution(
            activation,
            f"Holding station near the asset — {_distance(risk.path_length_m)} flown over "
            f"{_duration(risk.loiter_window_s)} for {_distance(risk.net_displacement_m)} of "
            "net travel.",
        )


class LowAltitudePenetrationFactor(EscalationFactor):
    """Low *and* close.

    Gated on proximity deliberately. Low altitude far out is unremarkable — it is most of
    general aviation, birds, and terrain clutter. Low altitude near the asset is
    terrain-following behaviour, and the combination is what carries the meaning rather than
    either half.
    """

    SUPPORTED_PARAMETERS = frozenset({"gate_range_m", "low_m", "very_low_m"})

    @property
    def code(self) -> str:
        return "low_altitude_near_asset"

    def assess(self, risk: TrackRisk, geometry: SiteGeometry) -> FactorContribution:
        low_m = self._policy.parameter("low_m", 120.0)
        very_low_m = self._policy.parameter("very_low_m", 30.0)
        gate_range_m = self._policy.parameter("gate_range_m", 1_500.0)

        distance = geometry.distance_to_protected_area_m(risk.position)
        if distance > gate_range_m:
            return self.silent("Not close enough for its altitude to be significant.")
        if risk.altitude_m > low_m:
            return self.silent(f"At {risk.altitude_m:.0f} m; not a low-altitude approach.")

        span = max(1e-6, low_m - very_low_m)
        activation = min(1.0, (low_m - risk.altitude_m) / span)
        return self.contribution(
            activation,
            f"Low approach — {risk.altitude_m:.0f} m altitude at {_distance(max(distance, 0.0))} "
            "from the protected area.",
        )


# --------------------------------------------------------------------------------------
# Identity and support
# --------------------------------------------------------------------------------------


class IdentityThreatFactor(EscalationFactor):
    """Classification believes this is an uncrewed aircraft.

    Uplift only, and that asymmetry is the point. A confident UAS identification raises the
    score; an unknown one leaves it exactly where the geometry put it. The discount for a
    *confidently benign* identification is applied once by the engine, not here, so that no
    factor can quietly reduce a score on its own.
    """

    @property
    def code(self) -> str:
        return "identified_as_uas"

    def assess(self, risk: TrackRisk, geometry: SiteGeometry) -> FactorContribution:
        if not risk.classification_assessed:
            return self.silent("Not yet classified; assessed on geometry alone.")

        mass = sum(risk.classification.get(label, 0.0) for label in UAS_LABELS)
        if mass <= 0.0:
            return self.silent("Classification does not indicate an uncrewed aircraft.")

        activation = mass * risk.classification_confidence
        leader = max(risk.classification, key=lambda label: risk.classification[label])
        return self.contribution(
            activation,
            f"Classified {str(leader).replace('_', ' ')} at "
            f"{risk.classification_confidence:.0%} confidence.",
        )


class PersistenceFactor(EscalationFactor):
    """How much of a track there actually is.

    Corroboration rather than a concern in its own right, and weighted as such. Its job is to
    stop a single noisy cycle reaching a high band on geometry that happens to look alarming
    for one update — and, equally, to let a contact that has been there for two minutes
    register as more than a blip.
    """

    SUPPORTED_PARAMETERS = frozenset({"established_s", "minimum_detections"})

    @property
    def code(self) -> str:
        return "persistent_track"

    def assess(self, risk: TrackRisk, geometry: SiteGeometry) -> FactorContribution:
        established_s = self._policy.parameter("established_s", 90.0)
        minimum_detections = self._policy.parameter("minimum_detections", 6.0)

        if risk.detection_count < minimum_detections:
            return self.silent(
                f"Only {risk.detection_count} detections; too little to call the track established."
            )

        activation = min(1.0, risk.track_age_s / max(1e-6, established_s))
        return self.contribution(
            activation,
            f"Tracked continuously for {_duration(risk.track_age_s)} across "
            f"{risk.detection_count} detections.",
        )


# --------------------------------------------------------------------------------------
# Mitigation — the only thing that may lower a score
# --------------------------------------------------------------------------------------


def mitigation_for(risk: TrackRisk, minimum: float) -> tuple[float, str | None]:
    """The discount earned by positive, confident evidence that this is not an air threat.

    Returns `(multiplier, reason)`, with the multiplier at 1.0 and no reason unless
    classification has actually identified something benign.

    Bounded below by `minimum` and never zero. Classification can be wrong, and a bird
    penetrating a protected zone is still a fact worth an operator's attention — a discount to
    nothing would delete the track from the operator's picture on the strength of a guess.
    """
    if not risk.classification_assessed:
        return 1.0, None

    benign_mass = sum(risk.classification.get(label, 0.0) for label in BENIGN_LABELS)
    belief = benign_mass * risk.classification_confidence
    if belief <= 0.0:
        return 1.0, None

    multiplier = 1.0 - belief * (1.0 - minimum)
    leader = max(BENIGN_LABELS, key=lambda label: risk.classification.get(label, 0.0))
    return (
        max(minimum, multiplier),
        f"Discounted: classified {str(leader).replace('_', ' ')} "
        f"({benign_mass:.0%} of the distribution at "
        f"{risk.classification_confidence:.0%} confidence).",
    )


# --------------------------------------------------------------------------------------
# Shared geometry helpers
#
# Public because `correlation.py` needs the same two questions answered — is this closing, and
# is it pointed at us — for clusters rather than tracks. Two implementations of "closing" that
# could drift apart is a worse outcome than a slightly wider surface here.
# --------------------------------------------------------------------------------------


def closure_rate_mps(risk: TrackRisk) -> float:
    """Speed along the line to the site origin. Positive closing, negative opening."""
    east, north = risk.position.east_m, risk.position.north_m
    distance = math.hypot(east, north)
    if distance <= 0.0:
        return 0.0

    # Radial component of velocity, negated because a shrinking range is a positive closure.
    return -((risk.velocity.east_mps * east + risk.velocity.north_mps * north) / distance)


def bearing_offset_deg(risk: TrackRisk) -> float | None:
    """Angle between the velocity vector and the bearing from the track to the site."""
    to_site_e, to_site_n = -risk.position.east_m, -risk.position.north_m
    if math.hypot(to_site_e, to_site_n) <= 0.0:
        return None

    course = math.atan2(risk.velocity.east_mps, risk.velocity.north_mps)
    bearing = math.atan2(to_site_e, to_site_n)
    difference = math.degrees(abs(course - bearing)) % 360.0
    return min(difference, 360.0 - difference)


def _clamp(value: float) -> float:
    if math.isnan(value):
        return 0.0
    return min(1.0, max(0.0, value))


def _distance(metres: float) -> str:
    return f"{metres / 1000:.1f} km" if metres >= 1000 else f"{metres:.0f} m"


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.0f} min"
