"""FM-014 — more objects than the system can carry.

A swarm, a flock of birds at dawn, or a sensor whose noise floor has collapsed into thousands of
spurious returns. ADR 0008 §2 sets the policy: bounded, never silently dropped, and degrading in
a chosen direction rather than an emergent one.

The chosen direction is the part worth testing. Under load the system must keep updating the
tracks an operator is already being asked to believe, and give up on *acquiring new ones* first.
That ordering is what makes saturation survivable: a system that shed uniformly would drop
updates to the confirmed inbound contact in order to keep initiating tracks on clutter, which is
precisely backwards.
"""

from __future__ import annotations

from backend.common.clock import SimClock
from backend.events.store import SqliteEventStore
from backend.events.types import EventType
from backend.tracking.models import TrackState
from backend.tracking.track_manager.manager import TrackManager

from .conftest import establish_confirmed_track, events_of, incoming, track_by_id


def flood(
    tracker: TrackManager, clock: SimClock, *, count: int, cycle: int, spacing_m: float = 400.0
) -> None:
    """One cycle carrying `count` mutually distant detections, each a new object."""
    clock.advance(1.0)
    tracker.process(
        [
            incoming(
                f"det-flood-{cycle}-{n}",
                at=clock.now(),
                east_m=-20_000.0 + n * spacing_m,
                north_m=15_000.0 + (n % 7) * spacing_m,
                sensor_id="radar_north",
            )
            for n in range(count)
        ]
    )


def test_the_active_track_set_is_bounded_under_an_unbounded_input(
    tracker: TrackManager, clock: SimClock
) -> None:
    """The floor of the whole failure mode.

    Without a ceiling, saturation is not a degraded mode — it is unbounded memory growth
    followed by the process being killed, which is an outage. A bound turns it into a state the
    system can be in and report on.
    """
    ceiling = tracker.config.max_active_tracks

    for cycle in range(4):
        flood(tracker, clock, count=ceiling, cycle=cycle)

    assert len(tracker.active_tracks()) <= ceiling


def test_nothing_is_shed_silently(
    tracker: TrackManager, clock: SimClock, events: SqliteEventStore
) -> None:
    """ADR 0008: every shed input is recorded.

    A dropped detection that leaves no trace makes saturation unreconstructable afterwards —
    the log would show a system tracking calmly through a period in which it was in fact
    discarding most of its input, and the two are indistinguishable without this event.
    """
    ceiling = tracker.config.max_active_tracks

    flood(tracker, clock, count=ceiling + 50, cycle=0)

    shed = events_of(events, EventType.DETECTION_SHED)
    assert shed, "detections were discarded without a record"
    assert all(event.payload.get("reason") for event in shed)
    assert all(event.payload.get("detection_id") for event in shed)


def test_an_established_track_keeps_being_updated_while_new_ones_are_refused(
    tracker: TrackManager, clock: SimClock
) -> None:
    """The ordering that makes saturation survivable, and the real point of this test file.

    The confirmed track here is the one the operator is watching. It must keep receiving its
    detections throughout, because association runs before initiation — so the capacity that
    runs out is the capacity to acquire *new* tracks, not the capacity to maintain existing
    ones. Shedding uniformly would drop updates to the inbound contact in order to keep
    initiating tracks on clutter.
    """
    track_id = establish_confirmed_track(tracker, clock)
    ceiling = tracker.config.max_active_tracks
    established = track_by_id(tracker, track_id)
    north_m = established.enu.north_m

    for cycle in range(3):
        clock.advance(1.0)
        north_m -= 20.0
        detections = [
            incoming(f"det-real-{cycle}", at=clock.now(), east_m=0.0, north_m=north_m)
        ] + [
            incoming(
                f"det-swarm-{cycle}-{n}",
                at=clock.now(),
                east_m=-20_000.0 + n * 400.0,
                north_m=15_000.0 + (n % 7) * 400.0,
            )
            for n in range(ceiling + 50)
        ]
        tracker.process(detections)

        maintained = track_by_id(tracker, track_id)
        assert maintained is not None, "the tracked contact was dropped under load"
        assert maintained.state is TrackState.CONFIRMED
        assert not maintained.tracking.is_coasting, (
            "the operator's contact went to coasting under load while its detection was present"
        )


def test_saturation_is_recoverable_rather_than_a_one_way_door(
    tracker: TrackManager, clock: SimClock
) -> None:
    """When the load goes away, the system must return to normal service.

    A ceiling that latched — leaving the active set permanently full of clutter that no longer
    exists — would turn a transient swarm into a permanent inability to acquire anything, which
    an operator would experience as the system never seeing anything again.
    """
    ceiling = tracker.config.max_active_tracks
    for cycle in range(3):
        flood(tracker, clock, count=ceiling + 50, cycle=cycle)

    assert len(tracker.active_tracks()) >= ceiling * 0.5

    # The swarm passes. Nothing is detected for long enough to retire the clutter.
    for _ in range(25):
        clock.advance(1.0)
        tracker.process([])

    assert tracker.active_tracks() == [], "clutter outlived the load that produced it"

    # And the system can acquire again afterwards.
    recovered = establish_confirmed_track(tracker, clock)
    assert track_by_id(tracker, recovered).state is TrackState.CONFIRMED
