"""Track model and lifecycle states.

A Track is the system's *belief* that a sequence of detections belong to one moving object.
It is persistent and evolving, where a Detection is a single instantaneous observation.
The operator interface displays tracks, never raw detections (spec §3).

Track state is held in the local ENU frame in metres (ADR 0004) and projected back to WGS-84
at the API boundary. The geodetic fields here are a convenience projection kept in step with
the ENU state, never an independent source of truth.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from sensors.schemas.detection import ClassificationLabel

TRACK_SCHEMA_VERSION = "1.0.0"


class TrackState(StrEnum):
    """Lifecycle state. A track is never silently deleted — it ends in LOST and is retained.

        NEW → TENTATIVE → CONFIRMED → STALE → LOST

    The transitions are deliberately explicit rather than implied by a detection count, so
    that every promotion and demotion is an event an operator can see and a replay can
    reconstruct.
    """

    NEW = "new"
    """First detection. One observation is not yet evidence of an object."""

    TENTATIVE = "tentative"
    """Accumulating support. May still turn out to be clutter, so it is not yet shown as a
    confident track."""

    CONFIRMED = "confirmed"
    """Enough consistent detections to believe an object is really there."""

    STALE = "stale"
    """Detections have stopped arriving for longer than coasting allows. The track is still
    displayed, but explicitly as degraded rather than as a confident position."""

    LOST = "lost"
    """No longer maintained. Retained in history rather than deleted (spec §5)."""

    @property
    def is_active(self) -> bool:
        """Whether the track is still being maintained by the tracker."""
        return self is not TrackState.LOST

    @property
    def is_displayable(self) -> bool:
        """Whether the operator should see it on the map.

        NEW is withheld: a single unconfirmed return is exactly what clutter looks like, and
        showing it would train operators to ignore the display.
        """
        return self in (TrackState.TENTATIVE, TrackState.CONFIRMED, TrackState.STALE)


class RiskLevel(StrEnum):
    """Discrete risk band. Hysteresis governs transitions between these (ADR 0007)."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def severity(self) -> int:
        return {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}[self.value]


class Recommendation(StrEnum):
    """What the system suggests a human consider doing.

    This is the end of the pipeline. Every value here is advice to an operator; none of them
    causes anything to happen in the physical world. `RESPONSE_RECOMMENDED` is the most
    severe output Aetos can produce, and it means exactly what it says — a recommendation
    that a human consider a response, decided and carried out by a separate system with its
    own authorisation and safety architecture.
    """

    MONITOR = "MONITOR"
    VERIFY = "VERIFY"
    ALERT = "ALERT"
    ESCALATE = "ESCALATE"
    RESPONSE_RECOMMENDED = "RESPONSE_RECOMMENDED"


class EnuState(BaseModel):
    """Position and velocity in the site's local ENU frame, metres and metres per second.

    This is the tracker's authoritative state. Velocity is *estimated by the filter from a
    sequence of positions*, never taken from a detection's kinematics block — see ADR 0002.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    east_m: float
    north_m: float
    up_m: float
    velocity_east_mps: float = 0.0
    velocity_north_mps: float = 0.0
    velocity_up_mps: float = 0.0


class TrackPosition(BaseModel):
    """Geodetic projection of the track's ENU state, for display and API output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float
    longitude: float
    altitude_m: float


class TrackKinematics(BaseModel):
    """Motion as derived by the filter. Speed and heading are computed from the ENU velocity
    estimate, so they are outputs of tracking rather than inputs to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    speed_mps: float = Field(ge=0.0)
    heading_deg: float = Field(ge=0.0, lt=360.0)
    vertical_rate_mps: float = 0.0


class TrackClassification(BaseModel):
    """What the classification module believes this track is. Confidence always reported
    separately from the label, never folded into it (spec §9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: ClassificationLabel = ClassificationLabel.UNKNOWN
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    distribution: dict[ClassificationLabel, float] = Field(default_factory=dict)
    """Probability across the label set. Empty when nothing has been assessed.

    `label` and `confidence` are derived from this rather than asserted beside it, so the
    headline can never disagree with the detail. A single label discards exactly what an
    operator needs when the system is unsure: `small_uas 0.55 / bird 0.40` and
    `small_uas 0.55 / medium_uas 0.40` have the same top-1 answer and very different
    operational meaning (ADR 0012)."""

    reasons: list[str] = Field(default_factory=list)
    """Human-readable justification, one per contributing evidence source. Carried for the
    same reason risk scores carry reason codes: a judgement an operator cannot interrogate is
    one they have to take on trust."""

    @property
    def assessed(self) -> bool:
        """Whether anything actually classified this track, as opposed to it holding the
        default. Distinguishes 'assessed as unknown' from 'never assessed'."""
        return bool(self.distribution)


class ThreatFactor(BaseModel):
    """One risk factor's reading, as published on a track.

    Declared here rather than in `backend/risk` for the same reason `TrackClassification` is:
    this is the *published* shape that rides in every track snapshot, and defining it in the
    producing module would make tracking depend on risk. The dependency has to point the other
    way — risk builds these, tracking only carries them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    weight: float = Field(ge=0.0, le=1.0)
    activation: float = Field(ge=0.0, le=1.0)
    contribution: float = Field(default=0.0, ge=0.0, le=1.0)
    detail: str


class TrackThreat(BaseModel):
    """Risk assessment for this track.

    `reasons` is not optional and not decorative. A bare risk score is a black-box output an
    operator cannot act on or challenge, so the score never travels without the human-readable
    reason codes that produced it (spec §10).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_score: int = Field(default=0, ge=0, le=100)
    """Raw and unsmoothed, as computed this cycle. Hysteresis applies to the level only, so
    the number never disagrees with the reasons attached to it (ADR 0007)."""

    risk_level: RiskLevel = RiskLevel.NONE

    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    """How much the assessment should be believed — from track support, freshness, coasting
    and whether classification has run. Deliberately never multiplied into the score: doing so
    would produce a number that is neither, and would quietly suppress the thin-evidence but
    alarming-geometry case that most needs an operator's eyes (ADR 0013)."""

    reasons: list[str] = Field(default_factory=list)
    """Human-readable, ordered by how much each factor actually moved the score."""

    codes: list[str] = Field(default_factory=list)
    """The machine-readable equivalent of `reasons`, in the same order. Safe to alert on and
    filter by across versions in a way a prose sentence is not."""

    factors: list[ThreatFactor] = Field(default_factory=list)
    """Every factor consulted, including those that found nothing. A score an operator cannot
    take apart is one they have to take on trust, and keeping the silent factors is what shows
    a concern was *checked* rather than merely absent."""

    recommendation: Recommendation = Recommendation.MONITOR

    assessed: bool = False
    """Whether risk assessment actually ran against this track.

    A score of zero from a module that ran means "we looked, and nothing here is concerning".
    A score of zero from one that did not means nothing at all. Those must never render the
    same way (ADR 0011)."""


class TrackFusion(BaseModel):
    """Which physical object this track is believed to be an observation of.

    Written by `backend/fusion` through the tracker, exactly as classification and risk are, so
    the snapshot on every track event carries it and the log rebuilds into the same state
    (ADR 0006, FM-025).

    The distinction this block exists to draw: **a track is an observation, an object is a
    thing.** Global nearest neighbour assigns at most one detection per track per cycle (ADR
    0005), which is correct and must stay that way — but it means three sensors watching one
    drone produce three tracks. Without something naming them as one object, an operator counts
    three contacts (FM-023).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    assessed: bool = False
    """Whether fusion has actually looked at this track.

    False means "not reconciled", which is different from "reconciled and found to stand
    alone". A track fusion has never seen must not be rendered as a confirmed single object
    (ADR 0011)."""

    object_id: str | None = None
    """Stable identifier for the physical object, `OBJ-NNNNN`. Distinct from `track_id`, and
    deliberately so: track identity belongs to the tracker and must not be rewritten by a
    downstream module."""

    is_primary: bool = True
    """Whether this is the track presented for its object.

    Every object has exactly one primary. The others are its supporting observations: still
    tracked, still in the log, but not counted or alerted on a second time. Defaults True so a
    track nothing has fused stands for itself rather than vanishing."""

    member_track_ids: list[str] = Field(default_factory=list)
    """Every track fused into this object, primary first. Populated on the primary only —
    duplicating the roster onto each member would make the same fact correctable in several
    places at once."""

    corroborating_sensors: list[str] = Field(default_factory=list)
    """Union of the contributing sensors across all members, sorted.

    On the primary this is the honest corroboration figure for the *object*, which is what risk
    should score. A member's own `tracking.contributing_sensors` describes only that one
    observation of it."""

    @property
    def member_count(self) -> int:
        return len(self.member_track_ids)

    @property
    def is_fused(self) -> bool:
        """Whether more than one track was reconciled into this object."""
        return len(self.member_track_ids) > 1


class TrackMetadata(BaseModel):
    """Provenance and support for the track — how much it is believed and on what basis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    first_seen: datetime
    last_seen: datetime
    """When the most recent *detection* arrived. Not the last update: a coasting track is
    updated by prediction without being seen, and conflating the two would let a coasted
    position look freshly observed."""

    detection_count: int = Field(default=0, ge=0)
    missed_updates: int = Field(default=0, ge=0)
    """Consecutive cycles with no associated detection. Drives coasting and demotion."""

    track_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    contributing_sensors: list[str] = Field(default_factory=list)

    @property
    def is_coasting(self) -> bool:
        return self.missed_updates > 0


class Track(BaseModel):
    """The system's belief about one moving object.

    Immutable: a track update produces a new Track rather than mutating one in place. This
    matters because tracks are published to subscribers and recorded in the event log, and a
    mutable object that changes after publication makes the log disagree with what was
    actually sent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = TRACK_SCHEMA_VERSION
    track_id: str = Field(min_length=1, max_length=64)
    state: TrackState

    enu: EnuState
    """Authoritative state, in the site's local frame."""

    position: TrackPosition
    """Geodetic projection of `enu`, for display. Kept in step with it, never independent."""

    kinematics: TrackKinematics
    classification: TrackClassification = Field(default_factory=TrackClassification)
    threat: TrackThreat = Field(default_factory=TrackThreat)
    fusion: TrackFusion = Field(default_factory=TrackFusion)
    tracking: TrackMetadata

    updated_at: datetime
    """When the tracker last produced this state, from the injected clock (ADR 0003)."""
