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

## Its input is objects, not tracks

This module used to cluster its own input, because `backend/fusion` did not exist and counting
raw tracks would have reported one drone seen by four sensors as a four-axis coordinated
approach — confident nonsense (FM-023). That clustering was always described as a stand-in, to
be deleted the moment fusion supplied the answer properly. Fusion exists now, so it is gone.

Everything reaching this module has already been reconciled: `backend/fusion` runs between
tracking and classification, and what arrives here is one track per physical object. Clustering
again would not merely be redundant, it would **disagree** — fusion holds a group together
across a hysteresis band (`split_separation_m`) precisely so the grouping does not chatter, so
two objects it has deliberately kept apart can sit closer than a single-threshold test would
merge them. Two subsystems answering "are these the same object" differently, in front of the
same operator, is worse than either answer alone.

So there is one authority on that question and this is not it.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field

from backend.risk.factors import TrackRisk, bearing_offset_deg, closure_rate_mps
from backend.tracking.models import RiskLevel
from backend.zones.geometry import SiteGeometry

SITUATION_SCHEMA_VERSION = "1.0.0"


class SituationPolicy(BaseModel):
    """Tuning for the cycle-level pass. Configuration, like everything else in this module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    distinct_bearing_deg: float = Field(default=35.0, gt=0.0)
    """How far apart two approach bearings must be to count as different axes. Below this,
    two contacts are arriving down the same corridor — which is a formation, not an
    envelopment, and reads very differently to an operator."""

    approach_bearing_tolerance_deg: float = Field(default=55.0, gt=0.0)
    """How closely an object's course must point at the site to count as approaching."""

    minimum_closure_mps: float = Field(default=2.0, ge=0.0)
    """Below this an object is not meaningfully closing, whatever its heading says."""

    coordinated_axes: int = Field(default=2, ge=2)
    """Distinct approach axes that constitute a coordinated approach."""

    saturation_objects: int = Field(default=4, ge=2)
    """Simultaneous engaged objects past which operator attention itself is the concern."""

    engagement_range_m: float = Field(default=5_000.0, gt=0.0)
    """Distance from the protected area within which an object counts as engaged."""


class SituationFinding(BaseModel):
    """One thing the picture as a whole says, that no single track could."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    level: RiskLevel
    detail: str
    track_ids: list[str] = Field(default_factory=list)
    """Every track involved, so an operator can select them and the interface can highlight
    them. Track identifiers rather than object identifiers, because the interface selects and
    highlights tracks — and after fusion each of these is the presented track of one object."""


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


def assess_situation(
    tracks: list[TrackRisk], geometry: SiteGeometry, policy: SituationPolicy
) -> SituationAssessment:
    """What this set of objects means together.

    `tracks` is one entry per physical object — `backend/fusion` guarantees it. The parameter
    keeps its name because a `TrackRisk` is still what arrives; what changed is that there is
    now exactly one of them per thing in the air.
    """
    if not tracks:
        return SituationAssessment(assessed=False)

    approaching = [track for track in tracks if _is_approaching(track, policy)]
    engaged = [
        track
        for track in tracks
        if geometry.distance_to_protected_area_m(track.position) <= policy.engagement_range_m
    ]

    findings: list[SituationFinding] = []

    axes = _distinct_axes(approaching, policy)
    if len(axes) >= policy.coordinated_axes:
        bearings = ", ".join(f"{bearing:03.0f}°" for bearing in sorted(axes))
        findings.append(
            SituationFinding(
                code="coordinated_approach",
                # Severity climbs with the number of axes, because an envelopment is harder to
                # attribute to coincidence the more directions it comes from.
                level=RiskLevel.CRITICAL if len(axes) >= 3 else RiskLevel.HIGH,
                detail=(
                    f"{_plural(len(axes), 'contact')} converging on the site from separate "
                    f"bearings ({bearings}). No single track shows this."
                ),
                track_ids=[track.track_id for track in approaching],
            )
        )

    if len(engaged) >= policy.saturation_objects:
        findings.append(
            SituationFinding(
                code="simultaneous_engagement",
                level=RiskLevel.HIGH,
                detail=(
                    f"{_plural(len(engaged), 'distinct object')} within "
                    f"{policy.engagement_range_m / 1000:.0f} km of the protected area at once."
                ),
                track_ids=[track.track_id for track in engaged],
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
        object_count=len(tracks),
        assessed=True,
    )


def _is_approaching(track: TrackRisk, policy: SituationPolicy) -> bool:
    if closure_rate_mps(track) < policy.minimum_closure_mps:
        return False
    offset = bearing_offset_deg(track)
    return offset is not None and offset <= policy.approach_bearing_tolerance_deg


def _distinct_axes(tracks: list[TrackRisk], policy: SituationPolicy) -> list[float]:
    """Approach bearings that are far enough apart to count as separate axes.

    Two contacts arriving down the same corridor are a formation, not an envelopment, and the
    difference is what an operator would act on differently.
    """
    axes: list[float] = []

    for track in tracks:
        # Bearing *from* the site to the contact: the direction the threat is coming from,
        # which is what an operator orients on.
        bearing = math.degrees(math.atan2(track.position.east_m, track.position.north_m)) % 360.0
        if all(_angular_gap(bearing, existing) >= policy.distinct_bearing_deg for existing in axes):
            axes.append(bearing)

    return axes


def _angular_gap(a: float, b: float) -> float:
    difference = abs(a - b) % 360.0
    return min(difference, 360.0 - difference)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """`count` and its noun, agreeing in number.

    These strings are read by an operator, not by a log parser. "1 objects" and "1 set(s)" are
    the sort of thing that quietly tells a reader the system was assembled carelessly, which is
    a bad impression for a display whose whole job is to be believed.
    """
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"
