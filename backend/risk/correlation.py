"""Situation assessment: what several tracks mean *together*.

Every factor in `factors.py` assesses one track alone, deliberately — the narrow input is what
stops a factor quietly depending on the previous risk level, which is how a score becomes
self-reinforcing. But some of the most important things an operator needs to know are not
properties of any single track. Three contacts converging on the asset from three bearings is
a different situation from three contacts that happen to be airborne, and no per-track factor
can tell them apart.

This runs **after** per-track scoring and produces a **separate finding**. It does not touch
any track's score. That separation is the whole design: counting nearby tracks inside a
per-track factor would make each track's score depend on how many other tracks exist, which is
exactly the coupling the narrow input exists to prevent.

## The duplicate-track problem, and why this can run before fusion exists

`backend/fusion` is not built, so one physical object observed by four sensors becomes up to
four tracks (FM-023). Counting tracks naively would report a single drone as a coordinated
four-axis approach — confident nonsense, and worse than saying nothing.

Fragments of one object and genuinely distinct contacts are separable, though, without doing
fusion's job. Fragments are **co-located and co-moving**: within sensor error of each other,
with near-identical velocity, because they are observations of the same thing. Distinct
contacts converging on an asset are the opposite — hundreds of metres apart, approaching along
different bearings. So tracks are clustered on that test, and the situation is assessed over
clusters rather than tracks.

This is **not fusion** and does not pretend to be. It merges nothing, changes no track state,
re-runs no association, and its output is scoped entirely to risk. It answers one question —
*how many distinct objects are these tracks likely to be* — because the situation assessment
cannot ask anything sensible without an answer to it. When `backend/fusion` exists it supplies
that answer properly and this clustering is deleted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from backend.risk.factors import TrackRisk, bearing_offset_deg, closure_rate_mps
from backend.tracking.models import RiskLevel
from backend.zones.geometry import SiteGeometry

SITUATION_SCHEMA_VERSION = "1.0.0"


class SituationPolicy(BaseModel):
    """Tuning for the cycle-level pass. Configuration, like everything else in this module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    duplicate_separation_m: float = Field(default=150.0, gt=0.0)
    """Horizontal separation below which two tracks may be the same object.

    Sized to sensor error rather than to object size. Two radars observing one drone at 4 km
    can disagree by a hundred metres or more in cross-range; two genuinely distinct drones
    flying that close together are, for the purposes of an approach assessment, one arrival
    from one bearing anyway — so the failure mode of an over-generous threshold is mild."""

    duplicate_velocity_mps: float = Field(default=6.0, gt=0.0)
    """Velocity difference below which two co-located tracks may be the same object. This is
    the discriminating half of the test: two distinct aircraft can pass within 150 m, but not
    while matching course and speed to within a few m/s."""

    distinct_bearing_deg: float = Field(default=35.0, gt=0.0)
    """How far apart two approach bearings must be to count as different axes. Below this,
    two contacts are arriving down the same corridor — which is a formation, not an
    envelopment, and reads very differently to an operator."""

    approach_bearing_tolerance_deg: float = Field(default=55.0, gt=0.0)
    """How closely a cluster's course must point at the site to count as approaching."""

    minimum_closure_mps: float = Field(default=2.0, ge=0.0)
    """Below this a cluster is not meaningfully closing, whatever its heading says."""

    coordinated_axes: int = Field(default=2, ge=2)
    """Distinct approach axes that constitute a coordinated approach."""

    saturation_clusters: int = Field(default=4, ge=2)
    """Simultaneous engaged objects past which operator attention itself is the concern."""

    engagement_range_m: float = Field(default=5_000.0, gt=0.0)
    """Distance from the protected area within which a cluster counts as engaged."""


@dataclass(slots=True)
class TrackCluster:
    """One or more tracks judged likely to be a single physical object."""

    track_ids: list[str] = field(default_factory=list)
    members: list[TrackRisk] = field(default_factory=list)

    @property
    def representative(self) -> TrackRisk:
        """The best-supported member, used wherever one position or velocity is needed.

        Best-supported rather than an average: averaging two tracks of one object produces a
        position no sensor reported, which is precisely the kind of invented measurement the
        rest of this system refuses to display.
        """
        return max(self.members, key=lambda member: member.detection_count)

    @property
    def is_fragmented(self) -> bool:
        return len(self.members) > 1


class SituationFinding(BaseModel):
    """One thing the picture as a whole says, that no single track could."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    level: RiskLevel
    detail: str
    track_ids: list[str] = Field(default_factory=list)
    """Every track involved, so an operator can select them and the interface can highlight
    them. Tracks, not clusters — clusters are this module's working representation and mean
    nothing outside it."""


class SituationAssessment(BaseModel):
    """The cycle-level finding, alongside but never folded into per-track risk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SITUATION_SCHEMA_VERSION

    level: RiskLevel = RiskLevel.NONE
    findings: list[SituationFinding] = Field(default_factory=list)

    track_count: int = 0
    object_count: int = 0
    """Distinct objects these tracks are likely to represent.

    Reported next to `track_count` rather than instead of it, because the gap between them is
    itself information: it is FM-023 made visible. An operator seeing "7 tracks, likely 3
    objects" knows not to read the display as seven aircraft."""

    assessed: bool = False

    @property
    def reasons(self) -> list[str]:
        return [finding.detail for finding in self.findings]

    @property
    def codes(self) -> list[str]:
        return [finding.code for finding in self.findings]


def cluster_tracks(tracks: list[TrackRisk], policy: SituationPolicy) -> list[TrackCluster]:
    """Group tracks that are probably the same physical object.

    Single-link agglomeration: a track joins a cluster if it is co-located and co-moving with
    *any* member. Single-link rather than complete-link because fragments of one object form a
    chain — sensor A and sensor C may each agree with B while disagreeing with each other by
    more than the threshold — and requiring every pair to agree would split that chain back
    into the fragments this exists to reconcile.
    """
    clusters: list[TrackCluster] = []

    for track in tracks:
        joined = next(
            (
                cluster
                for cluster in clusters
                if any(_probably_same_object(track, member, policy) for member in cluster.members)
            ),
            None,
        )
        if joined is None:
            clusters.append(TrackCluster(track_ids=[track.track_id], members=[track]))
        else:
            joined.track_ids.append(track.track_id)
            joined.members.append(track)

    return clusters


def _probably_same_object(a: TrackRisk, b: TrackRisk, policy: SituationPolicy) -> bool:
    separation = math.dist(
        (a.position.east_m, a.position.north_m, a.position.up_m),
        (b.position.east_m, b.position.north_m, b.position.up_m),
    )
    if separation > policy.duplicate_separation_m:
        return False

    velocity_difference = math.dist(
        (a.velocity.east_mps, a.velocity.north_mps, a.velocity.up_mps),
        (b.velocity.east_mps, b.velocity.north_mps, b.velocity.up_mps),
    )
    return velocity_difference <= policy.duplicate_velocity_mps


def assess_situation(
    tracks: list[TrackRisk], geometry: SiteGeometry, policy: SituationPolicy
) -> SituationAssessment:
    """What this set of tracks means together."""
    if not tracks:
        return SituationAssessment(assessed=False)

    clusters = cluster_tracks(tracks, policy)
    approaching = [
        cluster for cluster in clusters if _is_approaching(cluster.representative, policy)
    ]
    engaged = [
        cluster
        for cluster in clusters
        if geometry.distance_to_protected_area_m(cluster.representative.position)
        <= policy.engagement_range_m
    ]

    findings: list[SituationFinding] = []

    axes = _distinct_axes(approaching, policy)
    if len(axes) >= policy.coordinated_axes:
        bearings = ", ".join(f"{bearing:03.0f}°" for bearing in sorted(axes))
        involved = [track_id for cluster in approaching for track_id in cluster.track_ids]
        findings.append(
            SituationFinding(
                code="coordinated_approach",
                # Severity climbs with the number of axes, because an envelopment is harder to
                # attribute to coincidence the more directions it comes from.
                level=RiskLevel.CRITICAL if len(axes) >= 3 else RiskLevel.HIGH,
                detail=(
                    f"{len(axes)} contacts converging on the site from separate bearings "
                    f"({bearings}). No single track shows this."
                ),
                track_ids=involved,
            )
        )

    if len(engaged) >= policy.saturation_clusters:
        findings.append(
            SituationFinding(
                code="simultaneous_engagement",
                level=RiskLevel.HIGH,
                detail=(
                    f"{len(engaged)} distinct objects within "
                    f"{policy.engagement_range_m / 1000:.0f} km of the protected area at once."
                ),
                track_ids=[track_id for cluster in engaged for track_id in cluster.track_ids],
            )
        )

    fragmented = [cluster for cluster in clusters if cluster.is_fragmented]
    if fragmented:
        # Not a threat finding. An operator counting contacts off the display is entitled to
        # know the display is over-counting, and saying so is cheaper than letting them
        # discover it. Level NONE keeps it out of the situation's severity.
        findings.append(
            SituationFinding(
                code="duplicate_tracks_suspected",
                level=RiskLevel.NONE,
                detail=(
                    f"{len(tracks)} tracks appear to be {len(clusters)} objects — "
                    f"{len(fragmented)} set(s) are co-located and co-moving. Sensor fusion is "
                    "not built (FM-023)."
                ),
                track_ids=[track_id for cluster in fragmented for track_id in cluster.track_ids],
            )
        )

    return SituationAssessment(
        level=max(
            (finding.level for finding in findings),
            key=lambda level: level.severity,
            default=RiskLevel.NONE,
        ),
        findings=findings,
        track_count=len(tracks),
        object_count=len(clusters),
        assessed=True,
    )


def _is_approaching(track: TrackRisk, policy: SituationPolicy) -> bool:
    if closure_rate_mps(track) < policy.minimum_closure_mps:
        return False
    offset = bearing_offset_deg(track)
    return offset is not None and offset <= policy.approach_bearing_tolerance_deg


def _distinct_axes(clusters: list[TrackCluster], policy: SituationPolicy) -> list[float]:
    """Approach bearings that are far enough apart to count as separate axes.

    Two contacts arriving down the same corridor are a formation, not an envelopment, and the
    difference is what an operator would act on differently.
    """
    axes: list[float] = []

    for cluster in clusters:
        track = cluster.representative
        # Bearing *from* the site to the contact: the direction the threat is coming from,
        # which is what an operator orients on.
        bearing = math.degrees(math.atan2(track.position.east_m, track.position.north_m)) % 360.0
        if all(_angular_gap(bearing, existing) >= policy.distinct_bearing_deg for existing in axes):
            axes.append(bearing)

    return axes


def _angular_gap(a: float, b: float) -> float:
    difference = abs(a - b) % 360.0
    return min(difference, 360.0 - difference)
