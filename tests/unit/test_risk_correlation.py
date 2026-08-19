"""Situation assessment: what several objects mean together, and what they do not.

Its input is one entry per physical object — `backend/fusion` guarantees that, and the
clustering this module used to do as a stand-in has been deleted rather than left to disagree
with the module that now owns the question. The reconciliation tests live in
`tests/unit/test_fusion.py`.

What remains here is the property that matters most: **it must not touch per-track scores.**
Letting a cycle-level view adjust individual scores would make every score depend on how many
other objects happen to exist, which is the self-reinforcing coupling the narrow per-track
input exists to prevent.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from backend.events.store import SqliteEventStore
from backend.risk.correlation import SituationPolicy, assess_situation
from backend.risk.engine import build_risk_engine, load_risk_policy
from backend.risk.factors import TrackRisk
from backend.risk.service import RiskService
from backend.tracking.models import (
    EnuState,
    Track,
    TrackKinematics,
    TrackMetadata,
    TrackPosition,
    TrackState,
    TrackThreat,
)
from backend.zones.geometry import EnuPoint, EnuVelocity, SiteGeometry, load_site_geometry


@pytest.fixture(scope="module")
def geometry() -> SiteGeometry:
    return load_site_geometry("fictional_test_range")


@pytest.fixture
def policy() -> SituationPolicy:
    return SituationPolicy()


def contact(
    track_id: str,
    bearing_deg: float,
    range_m: float = 2_000.0,
    *,
    speed_mps: float = 20.0,
    inbound: bool = True,
    offset_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
    velocity_offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
    detection_count: int = 20,
) -> TrackRisk:
    """A track at `bearing_deg` from the site, flying towards it (or away)."""
    radians = math.radians(bearing_deg)
    east = math.sin(radians) * range_m
    north = math.cos(radians) * range_m

    direction = -1.0 if inbound else 1.0
    return TrackRisk(
        track_id=track_id,
        position=EnuPoint(east + offset_m[0], north + offset_m[1], 200.0 + offset_m[2]),
        velocity=EnuVelocity(
            direction * math.sin(radians) * speed_mps + velocity_offset[0],
            direction * math.cos(radians) * speed_mps + velocity_offset[1],
            velocity_offset[2],
        ),
        speed_mps=speed_mps,
        altitude_m=200.0,
        detection_count=detection_count,
        track_age_s=120.0,
        coasting=False,
        track_confidence=0.9,
    )


def codes(situation) -> set[str]:
    return set(situation.codes)


# --------------------------------------------------------------------------------------
# The duplicate-track problem: the reason this can run before fusion exists
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# Coordinated approach
# --------------------------------------------------------------------------------------


def test_contacts_converging_from_separate_bearings_are_reported(
    geometry: SiteGeometry, policy: SituationPolicy
) -> None:
    """The finding no per-track factor can produce."""
    situation = assess_situation(
        [contact("TRK-1", 0.0), contact("TRK-2", 120.0), contact("TRK-3", 240.0)],
        geometry,
        policy,
    )

    assert "coordinated_approach" in codes(situation)
    assert situation.object_count == 3
    finding = next(f for f in situation.findings if f.code == "coordinated_approach")
    assert set(finding.track_ids) == {"TRK-1", "TRK-2", "TRK-3"}


def test_more_axes_is_more_serious(geometry: SiteGeometry, policy: SituationPolicy) -> None:
    """An envelopment is harder to attribute to coincidence the more directions it comes from."""
    two = assess_situation([contact("TRK-1", 0.0), contact("TRK-2", 120.0)], geometry, policy)
    three = assess_situation(
        [contact("TRK-1", 0.0), contact("TRK-2", 120.0), contact("TRK-3", 240.0)],
        geometry,
        policy,
    )

    assert three.level.severity > two.level.severity


def test_contacts_down_one_corridor_are_a_formation_not_an_envelopment(
    geometry: SiteGeometry, policy: SituationPolicy
) -> None:
    """Two contacts arriving on nearly the same bearing are one axis, and read very differently
    to an operator than two contacts arriving on opposite sides."""
    situation = assess_situation(
        [contact("TRK-1", 40.0), contact("TRK-2", 50.0, range_m=3_000.0)], geometry, policy
    )

    assert "coordinated_approach" not in codes(situation)
    assert situation.object_count == 2


def test_contacts_that_are_not_closing_are_not_converging(
    geometry: SiteGeometry, policy: SituationPolicy
) -> None:
    situation = assess_situation(
        [
            contact("TRK-1", 0.0, inbound=False),
            contact("TRK-2", 120.0, inbound=False),
            contact("TRK-3", 240.0, inbound=False),
        ],
        geometry,
        policy,
    )

    assert "coordinated_approach" not in codes(situation)


def test_a_single_contact_is_never_a_coordinated_approach(
    geometry: SiteGeometry, policy: SituationPolicy
) -> None:
    situation = assess_situation([contact("TRK-1", 0.0)], geometry, policy)

    assert situation.findings == []
    assert situation.assessed is True


# --------------------------------------------------------------------------------------
# Saturation
# --------------------------------------------------------------------------------------


def test_many_objects_engaged_at_once_is_its_own_finding(
    geometry: SiteGeometry, policy: SituationPolicy
) -> None:
    """Past a point the operator's attention is itself the limiting factor, and that is worth
    saying even when no individual track is alarming."""
    spread = [
        contact(f"TRK-{i}", bearing, inbound=False) for i, bearing in enumerate([0, 90, 180, 270])
    ]

    situation = assess_situation(spread, geometry, policy)

    assert "simultaneous_engagement" in codes(situation)


def test_distant_objects_do_not_count_as_engaged(
    geometry: SiteGeometry, policy: SituationPolicy
) -> None:
    far = [
        contact(f"TRK-{i}", bearing, range_m=30_000.0, inbound=False)
        for i, bearing in enumerate([0, 90, 180, 270])
    ]

    situation = assess_situation(far, geometry, policy)

    assert "simultaneous_engagement" not in codes(situation)


# --------------------------------------------------------------------------------------
# Honesty
# --------------------------------------------------------------------------------------


def test_an_empty_picture_is_unassessed_rather_than_clear(
    geometry: SiteGeometry, policy: SituationPolicy
) -> None:
    """Nothing to assess is not the same as assessed-and-clear."""
    situation = assess_situation([], geometry, policy)

    assert situation.assessed is False
    assert situation.findings == []


def test_a_quiet_picture_is_assessed_and_clear(
    geometry: SiteGeometry, policy: SituationPolicy
) -> None:
    """The other half of the same distinction."""
    situation = assess_situation([contact("TRK-1", 0.0, inbound=False)], geometry, policy)

    assert situation.assessed is True
    assert situation.findings == []
    assert situation.level.severity == 0


def test_every_finding_names_the_tracks_it_is_about(
    geometry: SiteGeometry, policy: SituationPolicy
) -> None:
    """A finding an operator cannot trace to specific tracks is one they cannot act on."""
    situation = assess_situation(
        [contact("TRK-1", 0.0), contact("TRK-2", 120.0), contact("TRK-3", 240.0)],
        geometry,
        policy,
    )

    assert situation.findings
    for finding in situation.findings:
        assert finding.track_ids, f"{finding.code} named no tracks"


# --------------------------------------------------------------------------------------
# The separation: a cycle-level view must never move a per-track score
# --------------------------------------------------------------------------------------


def test_the_situation_pass_leaves_per_track_scores_untouched(
    geometry: SiteGeometry, policy: SituationPolicy
) -> None:
    """The property the whole two-pass design exists to preserve.

    A track's score must be a pure function of that track. If a cycle-level view could adjust
    it, every score would depend on how many other tracks happen to exist — so a busy sky would
    raise every score, which would raise the count of alarming tracks, which is a feedback loop
    with no evidence in it. The situation is therefore reported *alongside* per-track risk and
    never folded into it.

    Run through the real `RiskService`, not the engine: asking the engine the same question
    twice would prove nothing, since the situation pass is the thing under test and the engine
    never sees it.
    """
    clock = SimClock(datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC))

    def score_of(track_id: str, tracks: list[Track]) -> int:
        store = SqliteEventStore(clock)
        try:
            service = RiskService(
                build_risk_engine(),
                load_risk_policy(),
                geometry,
                store,
                sink=_Holder(tracks),
            )
            assessed = service.assess_cycle(tracks, clock.now())
            return next(t.threat.risk_score for t in assessed if t.track_id == track_id)
        finally:
            store.close()

    subject = _track("TRK-1", 0.0)
    crowd = [subject, _track("TRK-2", 120.0), _track("TRK-3", 240.0)]

    alone = score_of("TRK-1", [subject])
    in_crowd = score_of("TRK-1", crowd)

    assert alone == in_crowd, "a track's score changed because other tracks existed"


def _track(track_id: str, bearing_deg: float, range_m: float = 2_000.0) -> Track:
    """A published Track inbound from `bearing_deg`, for driving the service end to end."""
    radians = math.radians(bearing_deg)
    east, north = math.sin(radians) * range_m, math.cos(radians) * range_m
    speed = 20.0
    seen = datetime(2026, 8, 9, 11, 58, 0, tzinfo=UTC)

    return Track(
        track_id=track_id,
        state=TrackState.CONFIRMED,
        enu=EnuState(
            east_m=east,
            north_m=north,
            up_m=200.0,
            velocity_east_mps=-math.sin(radians) * speed,
            velocity_north_mps=-math.cos(radians) * speed,
        ),
        position=TrackPosition(latitude=45.0, longitude=0.0, altitude_m=200.0),
        kinematics=TrackKinematics(speed_mps=speed, heading_deg=(bearing_deg + 180.0) % 360.0),
        tracking=TrackMetadata(
            first_seen=seen, last_seen=seen, detection_count=20, track_confidence=0.9
        ),
        updated_at=seen,
    )


class _Holder:
    """Minimal `RiskSink`: stores the assessment and hands the track back.

    Standing in for `TrackManager` so the test exercises the service's own logic rather than
    the tracker's lifecycle, which has nothing to do with what is being asserted.
    """

    def __init__(self, tracks: list[Track]) -> None:
        self._tracks = {track.track_id: track for track in tracks}

    def apply_threat(self, track_id: str, threat: TrackThreat) -> Track | None:
        track = self._tracks.get(track_id)
        if track is None:
            return None
        updated = track.model_copy(update={"threat": threat})
        self._tracks[track_id] = updated
        return updated


# --------------------------------------------------------------------------------------
# Clustering must not depend on the order tracks were acquired in
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# Operator-facing wording
# --------------------------------------------------------------------------------------


