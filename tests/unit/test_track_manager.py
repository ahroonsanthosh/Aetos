"""Track lifecycle: promotion, coasting, demotion, reacquisition and saturation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from backend.common.clock import SimClock
from backend.common.geodesy import GeodeticPoint, LocalFrame
from backend.events.store import SqliteEventStore
from backend.events.types import EventType
from backend.tracking.filters.kalman import isotropic_covariance
from backend.tracking.models import TrackState
from backend.tracking.track_manager.manager import (
    IncomingDetection,
    TrackManager,
    TrackManagerConfig,
)

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
SITE = GeodeticPoint(53.35, -6.26, 0.0)
R = isotropic_covariance(10.0)


@pytest.fixture
def clock() -> SimClock:
    return SimClock(START)


@pytest.fixture
def events(clock: SimClock) -> SqliteEventStore:
    store = SqliteEventStore(clock)
    yield store
    store.close()


@pytest.fixture
def manager(clock: SimClock, events: SqliteEventStore) -> TrackManager:
    return TrackManager(clock, events, LocalFrame(SITE))


def detection(
    clock: SimClock,
    position: list[float],
    detection_id: str = "det-1",
    sensor: str = "SIM-RADAR-01",
) -> IncomingDetection:
    return IncomingDetection(
        detection_id=detection_id,
        sensor_id=sensor,
        timestamp=clock.now(),
        position_enu=np.array(position),
        covariance=R,
    )


def feed(manager: TrackManager, clock: SimClock, cycles: int, dt: float = 1.0, speed: float = 20.0):
    """Drive a single object east at a constant speed for `cycles` cycles."""
    tracks = []
    for i in range(cycles):
        tracks = manager.process([detection(clock, [i * speed * dt, 0.0, 100.0], f"det-{i}")])
        clock.advance(dt)
    return tracks


# --------------------------------------------------------------------------------------
# Promotion
# --------------------------------------------------------------------------------------


def test_a_single_detection_creates_a_track_but_does_not_display_it(
    manager: TrackManager, clock: SimClock
) -> None:
    """One unconfirmed return is exactly what clutter looks like. Showing it would train
    operators to ignore the display."""
    displayed = manager.process([detection(clock, [0.0, 0.0, 100.0])])

    assert displayed == []
    assert len(manager.active_tracks()) == 1
    assert manager.active_tracks()[0].state is TrackState.NEW


def test_a_second_detection_promotes_to_tentative(manager: TrackManager, clock: SimClock) -> None:
    tracks = feed(manager, clock, cycles=2)

    assert len(tracks) == 1
    assert tracks[0].state is TrackState.TENTATIVE


def test_sustained_detections_confirm_the_track(manager: TrackManager, clock: SimClock) -> None:
    tracks = feed(manager, clock, cycles=6)

    assert tracks[0].state is TrackState.CONFIRMED
    assert tracks[0].tracking.detection_count == 6


def test_one_object_produces_exactly_one_track(manager: TrackManager, clock: SimClock) -> None:
    """Track fragmentation would show the operator several objects where there is one."""
    feed(manager, clock, cycles=15)

    assert len(manager.active_tracks()) == 1


def test_track_confidence_grows_with_support(manager: TrackManager, clock: SimClock) -> None:
    early = feed(manager, clock, cycles=2)[0].tracking.track_confidence
    later = feed(manager, clock, cycles=6)[0].tracking.track_confidence

    assert later > early


def test_velocity_is_recovered_from_positions(manager: TrackManager, clock: SimClock) -> None:
    """The tracker is never told the object's speed or heading — ADR 0002. It must derive
    them, and it must get them roughly right, or every downstream risk judgement is wrong."""
    tracks = feed(manager, clock, cycles=25, dt=1.0, speed=20.0)

    assert tracks[0].kinematics.speed_mps == pytest.approx(20.0, abs=4.0)
    assert tracks[0].kinematics.heading_deg == pytest.approx(90.0, abs=10.0)  # due east


# --------------------------------------------------------------------------------------
# Clutter rejection
# --------------------------------------------------------------------------------------


def test_a_one_off_detection_never_confirms(manager: TrackManager, clock: SimClock) -> None:
    """Scenario F. A bird or an RF artefact appears once and is gone; it must not become a
    confirmed track an operator has to dismiss."""
    manager.process([detection(clock, [500.0, 500.0, 80.0])])
    clock.advance(1.0)

    for _ in range(4):
        manager.process([])
        clock.advance(1.0)

    assert manager.active_tracks() == []


def test_unsupported_tentative_tracks_are_retired_quickly(
    manager: TrackManager, clock: SimClock
) -> None:
    feed(manager, clock, cycles=2)

    for _ in range(manager.config.tentative_max_misses):
        manager.process([])
        clock.advance(1.0)

    assert manager.active_tracks() == []


# --------------------------------------------------------------------------------------
# Coasting, demotion and reacquisition
# --------------------------------------------------------------------------------------


def test_a_confirmed_track_coasts_through_a_brief_gap(
    manager: TrackManager, clock: SimClock
) -> None:
    """Scenario C. The track must survive a short dropout rather than fragmenting."""
    feed(manager, clock, cycles=8)

    tracks = manager.process([])
    clock.advance(2.0)

    assert tracks[0].state is TrackState.CONFIRMED
    assert tracks[0].tracking.is_coasting


def test_a_coasting_track_keeps_moving(manager: TrackManager, clock: SimClock) -> None:
    """Prediction, not a frozen last-known position. A stopped marker would misrepresent
    where the object actually is."""
    feed(manager, clock, cycles=10, dt=1.0, speed=20.0)
    before = manager.active_tracks()[0].enu.east_m

    manager.process([])
    clock.advance(3.0)
    manager.process([])

    assert manager.active_tracks()[0].enu.east_m > before


def test_a_prolonged_gap_demotes_to_stale(manager: TrackManager, clock: SimClock) -> None:
    """Past the coasting window the system must say so rather than keep presenting a
    confident position on data that stopped arriving."""
    feed(manager, clock, cycles=8)

    clock.advance(manager.config.coast_seconds + 1.0)
    manager.process([])

    assert manager.active_tracks()[0].state is TrackState.STALE


def test_a_stale_track_is_still_displayed(manager: TrackManager, clock: SimClock) -> None:
    """Marked degraded, not hidden. An object that vanished from sensors is exactly what an
    operator most needs to know about."""
    feed(manager, clock, cycles=8)
    clock.advance(manager.config.coast_seconds + 1.0)

    displayed = manager.process([])

    assert displayed[0].state is TrackState.STALE


def test_a_stale_track_eventually_becomes_lost(manager: TrackManager, clock: SimClock) -> None:
    feed(manager, clock, cycles=8)

    clock.advance(manager.config.coast_seconds + manager.config.stale_seconds + 1.0)
    manager.process([])

    assert manager.active_tracks() == []


def test_demotion_depends_on_elapsed_time_not_cycle_count(
    manager: TrackManager, clock: SimClock, events: SqliteEventStore
) -> None:
    """Regression. Advancing one lifecycle step per cycle made a track's survival depend on
    how often `process` happened to be called, so `coast_seconds` and `stale_seconds` did not
    mean the durations they name — a slow cycle rate silently kept dead tracks on screen.

    One cycle spanning both windows must retire the track, and must still record it passing
    through STALE on the way, or replay cannot reconstruct how it ended.
    """
    feed(manager, clock, cycles=8)
    track_id = manager.active_tracks()[0].track_id

    clock.advance(manager.config.coast_seconds + manager.config.stale_seconds + 1.0)
    manager.process([])

    assert manager.active_tracks() == []
    recorded = [e.event_type for e in events.read_for_track(track_id)]
    assert EventType.TRACK_STALE in recorded
    assert EventType.TRACK_LOST in recorded
    assert recorded.index(EventType.TRACK_STALE) < recorded.index(EventType.TRACK_LOST)


def test_a_slow_cycle_rate_does_not_extend_track_life(
    clock: SimClock, events: SqliteEventStore
) -> None:
    """The same regression from the other direction: two systems with different cycle rates
    must retire an unsupported track after the same elapsed time."""
    config = TrackManagerConfig()
    fast = TrackManager(clock, events, LocalFrame(SITE), config)

    feed(fast, clock, cycles=8)
    clock.advance(config.coast_seconds + config.stale_seconds + 1.0)
    fast.process([])

    assert fast.active_tracks() == []


def test_reacquisition_recovers_the_same_track(manager: TrackManager, clock: SimClock) -> None:
    """FM-022. An object seen again after a gap must not become a new track with a new ID —
    that fragments its history and shows the operator a 'new' object that is not new."""
    feed(manager, clock, cycles=10, dt=1.0, speed=20.0)
    original_id = manager.active_tracks()[0].track_id

    clock.advance(manager.config.coast_seconds + 1.0)
    manager.process([])
    assert manager.active_tracks()[0].state is TrackState.STALE

    # The object reappears where its motion says it should be.
    reacquired = manager.process([detection(clock, [10 * 20.0 + 7 * 20.0, 0.0, 100.0], "det-back")])

    assert len(reacquired) == 1
    assert reacquired[0].track_id == original_id
    assert reacquired[0].state is TrackState.CONFIRMED


def test_last_seen_does_not_advance_while_coasting(manager: TrackManager, clock: SimClock) -> None:
    """A coasted position must never look freshly observed. This is why TrackMetadata keeps
    last_seen separate from updated_at."""
    feed(manager, clock, cycles=8)
    last_seen = manager.active_tracks()[0].tracking.last_seen

    clock.advance(3.0)
    manager.process([])

    track = manager.active_tracks()[0]
    assert track.tracking.last_seen == last_seen
    assert track.updated_at > last_seen


def test_confidence_falls_while_coasting(manager: TrackManager, clock: SimClock) -> None:
    """Built from support and freshness, not from the filter's positional certainty — a
    coasting track's position estimate can look tidy while the evidence thins."""
    feed(manager, clock, cycles=8)
    before = manager.active_tracks()[0].tracking.track_confidence

    clock.advance(2.0)
    manager.process([])

    assert manager.active_tracks()[0].tracking.track_confidence < before


# --------------------------------------------------------------------------------------
# Multiple objects
# --------------------------------------------------------------------------------------


def test_multiple_objects_produce_stable_distinct_tracks(
    manager: TrackManager, clock: SimClock
) -> None:
    """Scenario D. Track IDs must be stable across cycles, or the operator sees objects
    apparently swapping identity."""
    ids_per_cycle = []

    for cycle in range(10):
        manager.process(
            [
                detection(clock, [cycle * 20.0, 0.0, 100.0], f"a-{cycle}"),
                detection(clock, [cycle * 20.0, 800.0, 120.0], f"b-{cycle}"),
                detection(clock, [cycle * 20.0, -800.0, 90.0], f"c-{cycle}"),
            ]
        )
        clock.advance(1.0)
        ids_per_cycle.append({t.track_id for t in manager.active_tracks()})

    assert len(ids_per_cycle[-1]) == 3
    assert ids_per_cycle[-1] == ids_per_cycle[-2]


def test_saturation_is_refused_loudly_not_silently(
    clock: SimClock, events: SqliteEventStore
) -> None:
    """Scenario I / ADR 0008. Past the ceiling, new track initiation stops and says so,
    rather than growing without bound until the process dies."""
    manager = TrackManager(clock, events, LocalFrame(SITE), TrackManagerConfig(max_active_tracks=5))

    manager.process([detection(clock, [i * 5_000.0, 0.0, 100.0], f"d-{i}") for i in range(12)])

    assert len(manager.active_tracks()) == 5
    shed = [e for e in events.read_from(1) if e.event_type is EventType.DETECTION_SHED]
    assert len(shed) == 7
    assert shed[0].payload["reason"] == "max_active_tracks_reached"


# --------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------


def test_every_lifecycle_transition_is_recorded(
    manager: TrackManager, clock: SimClock, events: SqliteEventStore
) -> None:
    """The log is the source of truth (ADR 0006), so a transition that leaves no event is a
    transition replay cannot reconstruct."""
    feed(manager, clock, cycles=8)
    clock.advance(manager.config.coast_seconds + 1.0)
    manager.process([])
    clock.advance(manager.config.stale_seconds + 1.0)
    manager.process([])

    recorded = [e.event_type for e in events.read_from(1)]

    assert EventType.TRACK_CREATED in recorded
    assert EventType.TRACK_CONFIRMED in recorded
    assert EventType.TRACK_STALE in recorded
    assert EventType.TRACK_LOST in recorded


def test_track_history_survives_the_track(
    manager: TrackManager, clock: SimClock, events: SqliteEventStore
) -> None:
    """Spec §5: a track is never silently deleted. It leaves active maintenance, but its
    whole history remains readable from the log."""
    feed(manager, clock, cycles=8)
    track_id = manager.active_tracks()[0].track_id

    clock.advance(manager.config.coast_seconds + manager.config.stale_seconds + 1.0)
    manager.process([])

    assert manager.active_tracks() == []
    assert len(list(events.read_for_track(track_id))) > 0


def test_events_carry_the_track_id(
    manager: TrackManager, clock: SimClock, events: SqliteEventStore
) -> None:
    feed(manager, clock, cycles=5)
    track_id = manager.active_tracks()[0].track_id

    assert all(e.track_id == track_id for e in events.read_for_track(track_id))


# --------------------------------------------------------------------------------------
# Geodetic projection
# --------------------------------------------------------------------------------------


def test_track_position_is_projected_back_to_wgs84(manager: TrackManager, clock: SimClock) -> None:
    """ENU is authoritative; the geodetic fields are a projection kept in step with it."""
    manager.process([detection(clock, [0.0, 0.0, 150.0])])

    track = manager.active_tracks()[0]

    assert track.position.latitude == pytest.approx(SITE.latitude_deg, abs=1e-6)
    assert track.position.longitude == pytest.approx(SITE.longitude_deg, abs=1e-6)
    assert track.position.altitude_m == pytest.approx(150.0, abs=0.1)


def test_contributing_sensors_are_recorded(manager: TrackManager, clock: SimClock) -> None:
    """Which sensors support a track is what makes degraded mode meaningful later."""
    for cycle in range(6):
        manager.process(
            [detection(clock, [cycle * 20.0, 0.0, 100.0], f"d-{cycle}", sensor="SIM-RADAR-01")]
        )
        clock.advance(1.0)

    manager.process([detection(clock, [120.0, 0.0, 100.0], "d-eo", sensor="SIM-EO-02")])

    assert manager.active_tracks()[0].tracking.contributing_sensors == [
        "SIM-EO-02",
        "SIM-RADAR-01",
    ]


def test_timestamps_come_from_the_injected_clock(manager: TrackManager, clock: SimClock) -> None:
    feed(manager, clock, cycles=3, dt=2.0)

    assert manager.active_tracks()[0].updated_at == START + timedelta(seconds=4)


# --------------------------------------------------------------------------------------
# Staleness is a duration, not a duration plus the sensor's clock offset
# --------------------------------------------------------------------------------------


def lagging_detection(
    clock: SimClock, position: list[float], detection_id: str, lag_s: float
) -> IncomingDetection:
    """A detection from a sensor whose clock reads `lag_s` behind the pipeline's."""
    return IncomingDetection(
        detection_id=detection_id,
        sensor_id="SIM-RADAR-01",
        timestamp=clock.now() - timedelta(seconds=lag_s),
        position_enu=np.array(position),
        covariance=np.eye(3) * 25.0,
    )


@pytest.mark.parametrize("sensor_lag_s", [0.0, 3.0, 10.0, 25.0])
def test_a_lagging_sensor_clock_does_not_shorten_the_coast(
    sensor_lag_s: float, manager: TrackManager, clock: SimClock
) -> None:
    """A confirmed track must coast for `coast_seconds` whatever its sensor's clock reads.

    Staleness used to be measured as `now - last_seen`: a pipeline instant minus a *sensor*
    instant, so the difference carried the skew between them. Ingestion accepts up to 30 s of
    skew (FM-012), so a sensor lagging 25 s produced confirmed tracks that were retired on
    their first missed cycle instead of coasting — the object reappeared as a new track with a
    new identity and a split history, which is the fragmentation `coast_seconds` exists to
    prevent (FM-022).

    The failure was silent and depended only on how well someone's sensors were synchronised.
    """
    for i in range(5):
        manager.process(
            [lagging_detection(clock, [i * 20.0, 0.0, 100.0], f"det-{i}", sensor_lag_s)]
        )
        clock.advance(1.0)

    assert manager.active_tracks()[0].state is TrackState.CONFIRMED

    manager.process([])  # one missed cycle, well inside the six-second coast budget

    surviving = manager.active_tracks()
    assert surviving, f"a track from a sensor lagging {sensor_lag_s}s was retired immediately"
    assert surviving[0].state is TrackState.CONFIRMED


def test_the_coast_budget_still_expires_on_its_own_terms(
    manager: TrackManager, clock: SimClock
) -> None:
    """The converse, so the fix cannot become "tracks never go stale": real elapsed time must
    still demote a track that has stopped being supported."""
    feed(manager, clock, cycles=5)

    clock.advance(manager.config.coast_seconds + 1.0)
    manager.process([])

    assert manager.active_tracks()[0].state is TrackState.STALE


def test_observation_time_is_still_reported_from_the_sensor(
    manager: TrackManager, clock: SimClock
) -> None:
    """The fix must not quietly restamp detections with receipt time: when the object was
    observed remains the sensor's answer, and only staleness moved to the pipeline clock."""
    observed_at = clock.now() - timedelta(seconds=8.0)

    manager.process([lagging_detection(clock, [0.0, 0.0, 100.0], "det-0", 8.0)])

    assert manager.active_tracks()[0].tracking.last_seen == observed_at


# --------------------------------------------------------------------------------------
# Duplicate tracks (ADR 0021)
# --------------------------------------------------------------------------------------


def lost_events(events: SqliteEventStore, reason: str | None = None):
    return [
        e
        for e in events.read_from(1)
        if e.event_type is EventType.TRACK_LOST
        and (reason is None or e.payload.get("reason") == reason)
    ]


def alternating_measurement(manager: TrackManager, clock: SimClock, turn: int, tag: str):
    """One measurement, placed on whichever of two tracks is due — the turn-taking observed live.

    Live, the two tracks of one object traded the single fused measurement because noise put it
    nearer one, then the other. Placing it on each track's own prediction in turn reproduces
    that exactly, without depending on a noise seed.
    """
    tracks = sorted(manager.active_tracks(), key=lambda t: t.track_id)
    target = tracks[turn % len(tracks)]
    position = [
        target.enu.east_m + target.enu.velocity_east_mps * 0.5,
        target.enu.north_m + target.enu.velocity_north_mps * 0.5,
        target.enu.up_m,
    ]
    return manager.process([detection(clock, position, f"{tag}-{turn}")])


def test_one_object_carried_by_two_tracks_is_reduced_to_one(
    manager: TrackManager, clock: SimClock, events: SqliteEventStore
) -> None:
    """The failure measured live: two tracks on one object, fed by one stream of measurements.

    The first cycle delivers two detections of the drone that do not associate together —
    which is how fusion's ambiguity rule leaves them — so two tracks start. From then on there
    is one measurement per cycle, as there is for one object, and the tracks can only share
    it. The younger must be retired, and the operator must keep the identifier they had.
    """
    manager.process(
        [
            detection(clock, [0.0, 0.0, 100.0], "det-a", sensor="RADAR-N"),
            detection(clock, [25.0, 0.0, 100.0], "det-b", sensor="RADAR-S"),
        ]
    )
    first, second = sorted(t.track_id for t in manager.active_tracks())
    clock.advance(0.5)

    for turn in range(1, 30):
        if len(manager.active_tracks()) < 2:
            break
        alternating_measurement(manager, clock, turn, "det")
        clock.advance(0.5)

    remaining = {t.track_id for t in manager.active_tracks()}
    assert remaining == {first}, f"expected only {first} to survive, have {remaining}"

    (retired,) = lost_events(events, "duplicate")
    assert retired.track_id == second
    assert retired.payload["duplicate_of"] == first
    assert retired.payload["shared_update_cycles"] <= 0.25 * retired.payload["evidence_cycles"]


def test_two_real_objects_flying_together_are_never_merged(
    manager: TrackManager, clock: SimClock, events: SqliteEventStore
) -> None:
    """The case a duplicate rule must not touch: formation flight inside the co-location gate.

    Two objects 15 m apart on the same course — statistically indistinguishable by position and
    velocity alone. What separates them is that each produces its own detection every cycle,
    so both tracks are updated together every cycle. Merging them would hide a contact, which
    is the worst thing this tracker can do.
    """
    from scipy.stats import chi2

    from backend.tracking.association.gnn import AssociationConfig
    from backend.tracking.track_manager.manager import _STATE_DOF, _same_state

    threshold = float(chi2.ppf(AssociationConfig().gate_confidence, _STATE_DOF))
    co_located_cycles = 0

    for i in range(60):
        manager.process(
            [
                detection(clock, [i * 10.0, 0.0, 100.0], f"lead-{i}", sensor="RADAR-N"),
                detection(clock, [i * 10.0, 15.0, 100.0], f"wing-{i}", sensor="RADAR-N"),
            ]
        )
        clock.advance(0.5)
        active = [t for t in manager._active.values()]
        if len(active) == 2 and _same_state(
            active[0].filter_state, active[1].filter_state, threshold
        ):
            co_located_cycles += 1

    assert co_located_cycles >= 30, (
        "the pair was not inside the co-location gate, so this test proved nothing"
    )
    assert lost_events(events, "duplicate") == []
    assert len(manager.active_tracks()) == 2


def test_a_brief_coincidence_does_not_accumulate_into_a_merge(
    manager: TrackManager, clock: SimClock, events: SqliteEventStore
) -> None:
    """Evidence must be continuous. Two tracks that meet, part and meet again must not add the
    two meetings together into one object."""
    from backend.tracking.track_manager.manager import TrackManagerConfig as Config

    needed = Config().duplicate_min_cycles
    manager.process(
        [
            detection(clock, [0.0, 0.0, 100.0], "a0", sensor="RADAR-N"),
            detection(clock, [20.0, 0.0, 100.0], "b0", sensor="RADAR-S"),
        ]
    )
    clock.advance(0.5)
    # Fewer cycles than the window of turn-taking, then two clearly separate objects.
    for turn in range(1, needed - 3):
        alternating_measurement(manager, clock, turn, "one")
        clock.advance(0.5)
    tracks_before = {t.track_id for t in manager.active_tracks()}
    assert len(tracks_before) == 2, "the setup did not produce the two tracks it needs"

    for i in range(needed - 3, needed + 20):
        manager.process(
            [
                detection(clock, [i * 10.0, 0.0, 100.0], f"x-{i}", sensor="RADAR-N"),
                detection(clock, [i * 10.0, 2_000.0, 100.0], f"y-{i}", sensor="RADAR-N"),
            ]
        )
        clock.advance(0.5)

    assert lost_events(events, "duplicate") == []


def test_two_short_meetings_do_not_add_up_to_one_object(
    manager: TrackManager, clock: SimClock, events: SqliteEventStore
) -> None:
    """Evidence must be continuous — driven directly, so the separation is exact.

    Two real objects that pass close twice can each time look like one stream for a few cycles
    (fusion merges their detections while they are inside sensor noise). Six cycles at each
    meeting is under the window; twelve across both would be over it. If evidence survived the
    separation in between, two real contacts would be merged into one.
    """
    manager.process(
        [
            detection(clock, [0.0, 0.0, 100.0], "a", sensor="RADAR-N"),
            detection(clock, [10.0, 0.0, 100.0], "b", sensor="RADAR-S"),
        ]
    )
    first, second = sorted(manager._active)
    window = TrackManagerConfig().duplicate_min_cycles
    meeting = window // 2 + 1

    def turns(count: int) -> None:
        for i in range(count):
            manager._retire_duplicates({first} if i % 2 == 0 else {second}, clock.now())

    turns(meeting)
    manager._active[second].filter_state.mean[1] += 5_000.0  # separate: far beyond any gate
    manager._retire_duplicates({first, second}, clock.now())
    manager._active[second].filter_state.mean[1] -= 5_000.0  # and meet again
    turns(meeting)

    assert 2 * meeting >= window, "the two meetings together must exceed the window"
    assert lost_events(events, "duplicate") == []


def test_a_track_coasting_through_a_dropout_is_never_taken_for_a_duplicate(
    manager: TrackManager, clock: SimClock, events: SqliteEventStore
) -> None:
    """Found by the ground-truth sweep, in the swarm scenario.

    A real object in a sensor dropout stops receiving detections and coasts on prediction. If
    it coasts next to a track that is still being fed, the pair looks like "almost never
    updated together" — because one of them is never updated at all. That is starvation, not
    turn-taking, and retiring the coasting track would hide a contact for exactly as long as the
    dropout lasts. Only two tracks that are each being fed, but not together, are one object.
    """
    manager.process(
        [
            detection(clock, [0.0, 0.0, 100.0], "a", sensor="RADAR-N"),
            detection(clock, [10.0, 0.0, 100.0], "b", sensor="RADAR-S"),
        ]
    )
    fed, coasting = sorted(manager._active)

    for _ in range(3 * TrackManagerConfig().duplicate_min_cycles):
        manager._retire_duplicates({fed}, clock.now())

    assert lost_events(events, "duplicate") == []
    assert coasting in manager._active


def test_the_survivor_keeps_its_identifier_and_takes_both_estimates(
    manager: TrackManager, clock: SimClock, events: SqliteEventStore
) -> None:
    """The older identifier survives — the one the operator has been watching — but not with
    its own estimate alone: a duplicate can form because the older track is lagging its object.
    The two took turns on mostly different measurements, so their estimates combine into one
    tighter than either, and the change is in the log so a rebuild matches what is held."""
    manager.process(
        [
            detection(clock, [0.0, 0.0, 100.0], "a", sensor="RADAR-N"),
            detection(clock, [20.0, 0.0, 100.0], "b", sensor="RADAR-S"),
        ]
    )
    older, younger = sorted(manager._active)
    before = {tid: manager._active[tid].filter_state.copy() for tid in (older, younger)}

    for i in range(3 * TrackManagerConfig().duplicate_min_cycles):
        if older not in manager._active or younger not in manager._active:
            break
        if manager._active[younger].state is TrackState.LOST:
            break
        before = {tid: manager._active[tid].filter_state.copy() for tid in (older, younger)}
        manager._retire_duplicates({older} if i % 2 == 0 else {younger}, clock.now())

    (retired,) = lost_events(events, "duplicate")
    assert retired.track_id == younger and retired.payload["duplicate_of"] == older

    survivor = manager._active[older].filter_state
    east = survivor.mean[0]
    low, high = sorted((before[older].mean[0], before[younger].mean[0]))
    assert low < east < high, "the survivor's position is not a combination of both estimates"
    assert np.trace(survivor.covariance) < min(
        np.trace(before[older].covariance), np.trace(before[younger].covariance)
    )

    absorbed = [
        e
        for e in events.read_from(1)
        if e.track_id == older and e.payload.get("reason") == "absorbed_duplicate"
    ]
    assert len(absorbed) == 1 and absorbed[0].payload["absorbed"] == younger
