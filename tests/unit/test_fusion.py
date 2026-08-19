"""Fusion: deciding which tracks are observations of one object.

The property this file exists to protect, stated once and tested from several directions:
**merging two distinct objects is worse than showing one object twice.**

Over-merging hides a contact — an operator sees one drone where there are two, and the one that
vanished is the one nobody is watching. Under-merging shows a contact twice, which is untidy and
costs trust but hides nothing. The thresholds are deliberately asymmetric because the failures
are, and scenario H — two aircraft crossing within sensor error of each other — is the case that
decides it.
"""

from __future__ import annotations

import itertools
import math
import time
from datetime import UTC, datetime, timedelta

import pytest

from backend.common.clock import SimClock
from backend.events.store import SqliteEventStore
from backend.events.types import EventType
from backend.fusion.policy import FusionPolicy
from backend.fusion.service import FusionService
from backend.tracking.models import (
    EnuState,
    Track,
    TrackFusion,
    TrackKinematics,
    TrackMetadata,
    TrackPosition,
    TrackState,
)

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> SimClock:
    return SimClock(START)


@pytest.fixture
def events(clock: SimClock) -> SqliteEventStore:
    store = SqliteEventStore(clock)
    yield store
    store.close()


@pytest.fixture
def policy() -> FusionPolicy:
    return FusionPolicy()


class RecordingSink:
    """Stands in for the tracker: remembers what fusion wrote, hands the track back.

    Returns a track carrying the fusion block, exactly as `TrackManager.apply_fusion` does, so
    the service is exercised against the contract it actually depends on rather than a stub that
    always agrees with it.
    """

    def __init__(self, tracks: dict[str, Track] | None = None) -> None:
        self.tracks = dict(tracks or {})
        self.applied: dict[str, TrackFusion] = {}
        self.missing: set[str] = set()

    def apply_fusion(self, track_id: str, fusion: TrackFusion) -> Track | None:
        self.applied[track_id] = fusion
        if track_id in self.missing:
            return None
        track = self.tracks.get(track_id)
        return track.model_copy(update={"fusion": fusion}) if track else None


def track(
    track_id: str,
    *,
    east: float = 0.0,
    north: float = 0.0,
    up: float = 100.0,
    velocity: tuple[float, float, float] = (0.0, -20.0, 0.0),
    detections: int = 20,
    sensors: tuple[str, ...] = ("radar_north",),
    age_s: float = 60.0,
) -> Track:
    speed = (velocity[0] ** 2 + velocity[1] ** 2) ** 0.5
    return Track(
        track_id=track_id,
        state=TrackState.CONFIRMED,
        enu=EnuState(
            east_m=east,
            north_m=north,
            up_m=up,
            velocity_east_mps=velocity[0],
            velocity_north_mps=velocity[1],
            velocity_up_mps=velocity[2],
        ),
        position=TrackPosition(latitude=53.35, longitude=-6.26, altitude_m=up),
        kinematics=TrackKinematics(speed_mps=speed, heading_deg=180.0, vertical_rate_mps=0.0),
        tracking=TrackMetadata(
            first_seen=START - timedelta(seconds=age_s),
            last_seen=START,
            detection_count=detections,
            track_confidence=0.9,
            contributing_sensors=list(sensors),
        ),
        updated_at=START,
    )


def service(
    events: SqliteEventStore, policy: FusionPolicy, tracks: list[Track]
) -> tuple[FusionService, RecordingSink]:
    sink = RecordingSink({t.track_id: t for t in tracks})
    return FusionService(policy, events, sink), sink


# --------------------------------------------------------------------------------------
# The safety property: distinct objects must never be merged
# --------------------------------------------------------------------------------------


def test_two_aircraft_crossing_are_never_merged(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """Scenario H, which is the case this whole module is constrained by.

    At the moment of a crossing two objects are co-located — closer than any separation
    threshold that would still reconcile real fragments. They are moving in opposite
    directions, and that is the only thing that separates them.
    """
    crossing = [
        track("TRK-00001", east=0.0, velocity=(20.0, 0.0, 0.0)),
        track("TRK-00002", east=10.0, velocity=(-20.0, 0.0, 0.0)),
    ]
    fusion, _ = service(events, policy, crossing)

    assert len(fusion.fuse_cycle(crossing)) == 2


def test_a_formation_flying_abreast_is_not_one_object(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """Co-moving is necessary but not sufficient: two drones in formation match course and
    speed exactly and are still two drones. Separation is what separates them."""
    formation = [
        track("TRK-00001", east=0.0),
        track("TRK-00002", east=policy.separation_m * 2.0),
    ]
    fusion, _ = service(events, policy, formation)

    assert len(fusion.fuse_cycle(formation)) == 2


def test_two_hovering_objects_are_not_merged_on_velocity_agreement(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """The one case where the discriminating half of the test stops discriminating.

    Two stationary objects both have near-zero velocity, so they agree trivially and separation
    alone would decide. Two drones loitering a hundred metres apart are two drones.
    """
    hovering = [
        track("TRK-00001", east=0.0, velocity=(0.0, 0.0, 0.0)),
        track("TRK-00002", east=100.0, velocity=(0.0, 0.0, 0.0)),
    ]
    fusion, _ = service(events, policy, hovering)

    assert len(fusion.fuse_cycle(hovering)) == 2


def test_a_single_return_is_not_fused_into_a_real_object(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """Clutter must not attach itself to a real track and inflate its corroboration.

    A track from one detection has a position seeded from a single measurement and no velocity
    estimate worth the name; its apparent agreement is not evidence.
    """
    tracks = [
        track("TRK-00001", east=0.0, detections=40),
        track("TRK-00002", east=10.0, detections=1, sensors=("radar_south",)),
    ]
    fusion, _ = service(events, policy, tracks)

    assert len(fusion.fuse_cycle(tracks)) == 2


# --------------------------------------------------------------------------------------
# The problem it exists for: FM-023
# --------------------------------------------------------------------------------------


def test_one_object_seen_by_three_sensors_is_one_object(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    fragments = [
        track("TRK-00001", east=0.0, sensors=("radar_north",)),
        track("TRK-00002", east=60.0, sensors=("radar_south",)),
        track("TRK-00003", east=120.0, sensors=("rf_central",)),
    ]
    fusion, _ = service(events, policy, fragments)

    presented = fusion.fuse_cycle(fragments)

    assert len(presented) == 1
    assert fusion.objects[0].member_ids == ("TRK-00001", "TRK-00002", "TRK-00003")


def test_the_object_carries_every_sensor_that_saw_it(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """The honest corroboration figure for the object. One member's own sensor list describes
    a single observation of it and understates what the site has seen."""
    fragments = [
        track("TRK-00001", east=0.0, sensors=("radar_north",)),
        track("TRK-00002", east=60.0, sensors=("radar_south", "rf_central")),
    ]
    fusion, sink = service(events, policy, fragments)
    fusion.fuse_cycle(fragments)

    assert fusion.objects[0].corroborating_sensors == (
        "radar_north",
        "radar_south",
        "rf_central",
    )
    assert sink.applied["TRK-00001"].corroborating_sensors == [
        "radar_north",
        "radar_south",
        "rf_central",
    ]


def test_supporting_members_are_marked_not_deleted(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """Members stay tracked and stay in the log. An operator asking why an object shows three
    tracks is entitled to all three."""
    fragments = [track("TRK-00001", east=0.0), track("TRK-00002", east=60.0)]
    fusion, sink = service(events, policy, fragments)
    fusion.fuse_cycle(fragments)

    assert sink.applied["TRK-00001"].is_primary is True
    assert sink.applied["TRK-00002"].is_primary is False
    assert sink.applied["TRK-00002"].object_id == sink.applied["TRK-00001"].object_id
    # The roster lives on the primary only, so the same fact is not correctable in two places.
    assert sink.applied["TRK-00002"].member_track_ids == []


def test_fusion_never_alters_a_track_estimate(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """Every coordinate presented is one the tracker actually produced.

    The primary is *chosen*, never synthesised: a covariance-weighted mean of two estimates is
    statistically better than either and is still a position no sensor reported, and this system
    does not display those.
    """
    fragments = [
        track("TRK-00001", east=0.0, north=1000.0),
        track("TRK-00002", east=100.0, north=1000.0),
    ]
    fusion, _ = service(events, policy, fragments)

    presented = fusion.fuse_cycle(fragments)[0]
    original = next(f for f in fragments if f.track_id == presented.track_id)

    assert presented.enu.east_m == original.enu.east_m
    assert presented.enu.north_m == original.enu.north_m
    assert presented.kinematics.speed_mps == original.kinematics.speed_mps


# --------------------------------------------------------------------------------------
# Grouping: chains, order independence
# --------------------------------------------------------------------------------------


def test_a_chain_of_fragments_groups_the_same_whatever_the_acquisition_order(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """Single-link agglomeration, and the reason it is single-link.

    Sensor A and sensor C may each agree with B while disagreeing with each other by more than
    the threshold. Requiring every pair to agree would split that chain back into the fragments
    this module exists to reconcile — and joining only the first matching group would make the
    answer depend on the order the sensors happened to acquire, which is nothing.
    """
    step = policy.separation_m * 0.9
    chain = [
        track("TRK-00001", east=0.0),
        track("TRK-00002", east=step),
        track("TRK-00003", east=2 * step),
    ]

    for ordering in itertools.permutations(chain):
        fusion, _ = service(events, policy, list(ordering))
        presented = fusion.fuse_cycle(list(ordering))
        assert len(presented) == 1, f"order {[t.track_id for t in ordering]} split the chain"


def test_grouping_does_not_swallow_a_distant_contact(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    tracks = [
        track("TRK-00001", east=0.0),
        track("TRK-00002", east=60.0),
        track("TRK-00003", east=5_000.0),
    ]
    fusion, _ = service(events, policy, tracks)

    assert len(fusion.fuse_cycle(tracks)) == 2


# --------------------------------------------------------------------------------------
# Stability across cycles — the part an operator sees
# --------------------------------------------------------------------------------------


def test_the_object_keeps_its_identity_when_a_sensor_joins(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """A fourth sensor acquiring the drone must not renumber it."""
    first = [track("TRK-00001", east=0.0), track("TRK-00002", east=60.0)]
    fusion, sink = service(events, policy, first)
    fusion.fuse_cycle(first)
    original = fusion.objects[0].object_id

    later = [*first, track("TRK-00003", east=120.0, sensors=("rf_central",))]
    sink.tracks = {t.track_id: t for t in later}
    fusion.fuse_cycle(later)

    assert fusion.objects[0].object_id == original


def test_the_presented_track_does_not_flicker_between_members(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """Recomputing the primary every cycle would hand it to whichever member is marginally
    best supported at that instant, and detection counts cross constantly.

    Everything keyed by track id travels with it — risk hysteresis above all, whose dwell timers
    would reset mid-escalation.
    """
    fusion, sink = service(events, policy, [])
    primaries = []

    for cycle in range(6):
        # Detection counts cross on every cycle, which is exactly what used to move the primary.
        tracks = [
            track("TRK-00001", east=0.0, detections=20 + cycle),
            track("TRK-00002", east=60.0, detections=25 - cycle),
        ]
        sink.tracks = {t.track_id: t for t in tracks}
        primaries.append(fusion.fuse_cycle(tracks)[0].track_id)

    assert len(set(primaries)) == 1, f"primary moved between members: {primaries}"


def test_a_split_group_does_not_leave_two_objects_sharing_one_identity(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """Regression, and the one that mattered most.

    Both halves of a split group still carry the old object id, so both inherited it. Two groups
    then held the same identity, the incumbent-primary record was overwritten by whichever was
    processed last, and the presented track flipped every cycle. Because alerts key on the
    presented track, each flip cleared one alert and raised another: one drone produced 34
    alerts where the unfused pipeline produced 3.
    """
    together = [track("TRK-00001", east=0.0), track("TRK-00002", east=60.0)]
    fusion, sink = service(events, policy, together)
    fusion.fuse_cycle(together)

    apart = [
        track("TRK-00001", east=0.0),
        track("TRK-00002", east=policy.split_separation_m * 3.0),
    ]
    sink.tracks = {t.track_id: t for t in apart}
    fusion.fuse_cycle(apart)

    ids = [obj.object_id for obj in fusion.objects]
    assert len(fusion.objects) == 2
    assert len(set(ids)) == 2, f"two objects share one identity: {ids}"


def test_a_fused_pair_holds_together_across_the_hysteresis_band(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """Once fused, a pair stays fused until it separates by clearly more than it merged at.

    Without the band the grouping chatters: fragments sit near the merge threshold and drift
    across it, and every re-form clears and re-raises the object's alert. Same Schmitt trigger
    as ADR 0007 applies to risk levels, against the same failure.
    """
    together = [track("TRK-00001", east=0.0), track("TRK-00002", east=60.0)]
    fusion, sink = service(events, policy, together)
    fusion.fuse_cycle(together)

    # Beyond the merge threshold but inside the split threshold: still one object.
    drifted = [
        track("TRK-00001", east=0.0),
        track("TRK-00002", east=(policy.separation_m + policy.split_separation_m) / 2.0),
    ]
    sink.tracks = {t.track_id: t for t in drifted}

    assert len(fusion.fuse_cycle(drifted)) == 1

    # Past the split threshold: genuinely separated.
    gone = [
        track("TRK-00001", east=0.0),
        track("TRK-00002", east=policy.split_separation_m * 1.5),
    ]
    sink.tracks = {t.track_id: t for t in gone}

    assert len(fusion.fuse_cycle(gone)) == 2


def test_a_pair_that_was_never_fused_must_earn_the_merge_threshold(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """The band works in one direction only. Two tracks that have never been together are held
    to the tighter threshold, so the band cannot be used to merge something it never should
    have."""
    apart = [
        track("TRK-00001", east=0.0),
        track("TRK-00002", east=(policy.separation_m + policy.split_separation_m) / 2.0),
    ]
    fusion, _ = service(events, policy, apart)

    assert len(fusion.fuse_cycle(apart)) == 2


# --------------------------------------------------------------------------------------
# Recording, and honest reporting of what has not been assessed
# --------------------------------------------------------------------------------------


def test_a_fused_object_is_recorded_once_not_every_cycle(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """The fusion state travels on every track snapshot anyway. What deserves its own entry is
    the moment the system decided two contacts were one."""
    fragments = [track("TRK-00001", east=0.0), track("TRK-00002", east=60.0)]
    fusion, _ = service(events, policy, fragments)

    for _ in range(5):
        fusion.fuse_cycle(fragments)

    recorded = [e for e in events.read_from(1) if e.event_type is EventType.TRACKS_FUSED]
    assert len(recorded) == 1
    assert recorded[0].payload["member_track_ids"] == ["TRK-00001", "TRK-00002"]


def test_a_lone_track_records_nothing_and_stands_for_itself(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    alone = [track("TRK-00001", east=0.0)]
    fusion, sink = service(events, policy, alone)

    presented = fusion.fuse_cycle(alone)

    assert len(presented) == 1
    assert sink.applied["TRK-00001"].is_primary is True
    assert sink.applied["TRK-00001"].is_fused is False
    assert [e for e in events.read_from(1) if e.event_type is EventType.TRACKS_FUSED] == []


def test_an_unfused_track_is_marked_unassessed_by_default() -> None:
    """A track fusion has never seen must not read as one it examined and left alone (ADR
    0011)."""
    assert TrackFusion().assessed is False
    assert track("TRK-00001").fusion.assessed is False


def test_a_track_retired_mid_cycle_is_dropped_rather_than_presented(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """Retired between tracking and fusion is stale, not an error."""
    tracks = [track("TRK-00001", east=0.0), track("TRK-00002", east=9_000.0)]
    fusion, sink = service(events, policy, tracks)
    sink.missing.add("TRK-00001")

    presented = fusion.fuse_cycle(tracks)

    assert [t.track_id for t in presented] == ["TRK-00002"]


# --------------------------------------------------------------------------------------
# Hostile and degenerate input
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_position_never_merges_anything(
    events: SqliteEventStore, policy: FusionPolicy, bad: float
) -> None:
    """`NaN > limit` is False, so an unguarded separation test *accepts* a NaN position.

    Found by pentest: two tracks merged into one object on the strength of a coordinate that
    was not a number, which is the failure direction that hides a contact. Refusing to fuse is
    the safe reading of "we do not know where this is".
    """
    tracks = [track("TRK-00001", east=0.0), track("TRK-00002", east=bad)]
    fusion, _ = service(events, policy, tracks)

    assert len(fusion.fuse_cycle(tracks)) == 2


def test_a_chain_of_tracks_cannot_grow_without_limit(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """Single-link grouping chains, and across a swarm that is catastrophic.

    Found by pentest: 400 tracks spaced 10 m apart — each within the merge threshold of the
    next — collapsed into **one** object spanning 4 km. On `i_swarm_saturation` the same effect
    produced a genuine object 1497 m across, against 327 m for the widest real fusion anywhere
    else. Under-counting is the dangerous direction: an object that vanished into another is
    one nobody is watching.
    """
    chain = [track(f"TRK-{i:05d}", east=i * 10.0) for i in range(200)]
    fusion, _ = service(events, policy, chain)

    objects = fusion.fuse_cycle(chain)

    assert len(objects) > 1, "a 2 km chain collapsed into one object"
    for obj in fusion.objects:
        positions = [(m.enu.east_m, m.enu.north_m, m.enu.up_m) for m in obj.members]
        extent = max(
            (
                math.dist(a, b)
                for index, a in enumerate(positions)
                for b in positions[index + 1 :]
            ),
            default=0.0,
        )
        assert extent <= policy.max_object_extent_m


def test_fusing_the_track_ceiling_stays_within_a_cycle(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    """The grouping is quadratic in the worst case, and the tracker admits 200 tracks.

    Pinned because a fusion pass that outran the pipeline's tick would push the whole cycle
    late, and the cost is paid every cycle rather than once.
    """
    tracks = [track(f"TRK-{i:05d}", east=i * 10.0) for i in range(200)]
    fusion, _ = service(events, policy, tracks)

    started = time.perf_counter()
    fusion.fuse_cycle(tracks)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.25, f"fusion took {elapsed * 1000:.0f} ms for 200 tracks"


def test_an_empty_cycle_is_not_an_error(
    events: SqliteEventStore, policy: FusionPolicy
) -> None:
    fusion, _ = service(events, policy, [])
    assert fusion.fuse_cycle([]) == []
    assert fusion.objects == []


def test_a_policy_whose_band_is_inverted_is_refused() -> None:
    """A split threshold below the merge threshold would separate tracks at a distance that
    immediately re-merges them — the chatter the band exists to prevent, encoded in config."""
    with pytest.raises(ValueError, match="split_separation_m"):
        FusionPolicy(separation_m=200.0, split_separation_m=100.0)
    with pytest.raises(ValueError, match="split_velocity_mps"):
        FusionPolicy(velocity_mps=10.0, split_velocity_mps=5.0)
