"""Situation assessment: what several tracks mean together, and what they do not.

Two properties matter more than the rest, and most of this file is about them.

**It must not cry wolf on duplicate tracks.** Sensor fusion is not built, so one object seen by
four sensors becomes up to four tracks (FM-023). Counting tracks naively would report a single
drone as a four-axis coordinated approach — confident nonsense, and worse than saying nothing.

**It must not touch per-track scores.** Letting a cycle-level view adjust individual scores
would make every score depend on how many other tracks happen to exist, which is the
self-reinforcing coupling the narrow per-track input exists to prevent.
"""

from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from backend.events.store import SqliteEventStore
from backend.risk.correlation import (
    SituationPolicy,
    assess_situation,
    cluster_tracks,
)
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


def test_one_object_seen_by_four_sensors_is_one_object(policy: SituationPolicy) -> None:
    """Four tracks of the same drone, disagreeing by sensor error, are not four contacts."""
    fragments = [
        contact("TRK-1", 45.0),
        contact("TRK-2", 45.0, offset_m=(60.0, -40.0, 5.0), velocity_offset=(1.5, 0.8, 0.0)),
        contact("TRK-3", 45.0, offset_m=(-70.0, 30.0, -8.0), velocity_offset=(-2.0, 1.1, 0.0)),
        contact("TRK-4", 45.0, offset_m=(20.0, 90.0, 2.0), velocity_offset=(0.5, -1.9, 0.0)),
    ]

    clusters = cluster_tracks(fragments, policy)

    assert len(clusters) == 1
    assert set(clusters[0].track_ids) == {"TRK-1", "TRK-2", "TRK-3", "TRK-4"}


def test_fragments_of_one_object_raise_no_coordinated_approach(
    geometry: SiteGeometry, policy: SituationPolicy
) -> None:
    """The failure this module would be worthless without avoiding."""
    fragments = [
        contact("TRK-1", 45.0),
        contact("TRK-2", 45.0, offset_m=(60.0, -40.0, 0.0), velocity_offset=(1.5, 0.8, 0.0)),
        contact("TRK-3", 45.0, offset_m=(-70.0, 30.0, 0.0), velocity_offset=(-2.0, 1.1, 0.0)),
    ]

    situation = assess_situation(fragments, geometry, policy)

    assert "coordinated_approach" not in codes(situation)
    assert situation.object_count == 1
    assert situation.track_count == 3


def test_over_counting_is_reported_rather_than_hidden(
    geometry: SiteGeometry, policy: SituationPolicy
) -> None:
    """An operator counting contacts off the display is entitled to know it over-counts."""
    fragments = [
        contact("TRK-1", 45.0),
        contact("TRK-2", 45.0, offset_m=(60.0, -40.0, 0.0), velocity_offset=(1.0, 0.5, 0.0)),
    ]

    situation = assess_situation(fragments, geometry, policy)

    assert "duplicate_tracks_suspected" in codes(situation)
    # Reported, but not as a threat: it says nothing about danger.
    finding = next(f for f in situation.findings if f.code == "duplicate_tracks_suspected")
    assert finding.level.severity == 0


def test_co_located_but_diverging_tracks_stay_separate(policy: SituationPolicy) -> None:
    """Position alone is not enough. Two aircraft can pass within 150 m; they cannot do it
    while matching course and speed, and velocity is the discriminating half of the test."""
    crossing = [
        contact("TRK-1", 45.0),
        contact("TRK-2", 45.0, offset_m=(50.0, 0.0, 0.0), velocity_offset=(0.0, 40.0, 0.0)),
    ]

    assert len(cluster_tracks(crossing, policy)) == 2


def test_far_apart_tracks_stay_separate_however_similar_their_motion(
    policy: SituationPolicy,
) -> None:
    """Two drones flying the same course a kilometre apart are two drones."""
    parallel = [contact("TRK-1", 45.0), contact("TRK-2", 45.0, offset_m=(1_000.0, 0.0, 0.0))]

    assert len(cluster_tracks(parallel, policy)) == 2


def test_clustering_chains_through_a_common_member(policy: SituationPolicy) -> None:
    """Single-link on purpose: fragments of one object form a chain, where the outermost two
    disagree by more than the threshold while each agrees with the middle. Requiring every
    pair to agree would split the chain back into the fragments this exists to reconcile."""
    chain = [
        contact("TRK-1", 45.0),
        contact("TRK-2", 45.0, offset_m=(140.0, 0.0, 0.0)),
        contact("TRK-3", 45.0, offset_m=(280.0, 0.0, 0.0)),
    ]

    clusters = cluster_tracks(chain, policy)

    assert len(clusters) == 1
    assert len(clusters[0].track_ids) == 3


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


def test_a_chain_of_fragments_clusters_the_same_whatever_the_acquisition_order(
    policy: SituationPolicy,
) -> None:
    """The exact case single-link agglomeration is chosen for, from every direction.

    Regression: a track was joined to the *first* cluster it matched and never merged the
    others, so the chain the docstring describes — sensor A and sensor C each agreeing with B
    while disagreeing with each other — survived only when B happened to be processed between
    them. With A and C acquired first they opened two clusters, and B arriving later joined one
    and left the other stranded.

    Acquisition order is whatever the sensors happened to do, so the same three fragments of
    one drone were reported as one object or two depending on nothing meaningful. Being the
    connected components of the relation, the answer must not move.
    """
    separation = policy.duplicate_separation_m * 0.9
    chain = [
        contact("TRK-A", bearing_deg=0.0, offset_m=(0.0, 0.0, 0.0)),
        contact("TRK-B", bearing_deg=0.0, offset_m=(separation, 0.0, 0.0)),
        contact("TRK-C", bearing_deg=0.0, offset_m=(2 * separation, 0.0, 0.0)),
    ]

    for ordering in itertools.permutations(chain):
        clusters = cluster_tracks(list(ordering), policy)
        acquired = [track.track_id for track in ordering]

        assert len(clusters) == 1, (
            f"acquisition order {acquired} split one chain of fragments into "
            f"{len(clusters)} objects"
        )
        assert sorted(clusters[0].track_ids) == ["TRK-A", "TRK-B", "TRK-C"]


def test_merging_a_chain_does_not_swallow_genuinely_distinct_contacts(
    policy: SituationPolicy,
) -> None:
    """The merge must stay bounded by the relation — two contacts far apart are two objects,
    however the clusters were built up around them."""
    far_apart = [
        contact("TRK-A", bearing_deg=0.0, range_m=2_000.0),
        contact("TRK-B", bearing_deg=180.0, range_m=2_000.0),
    ]

    for ordering in itertools.permutations(far_apart):
        clusters = cluster_tracks(list(ordering), policy)
        assert len(clusters) == 2
