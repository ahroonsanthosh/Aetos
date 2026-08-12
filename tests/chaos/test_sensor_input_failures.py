"""FM-010, FM-011, FM-012, FM-013 — the sensor lies, repeats itself, or stops talking.

Everything upstream of the tracker is untrusted. A sensor is a physical device on a network:
it drops out, it double-sends, its clock drifts, and its firmware emits a field nobody expected.
None of that is exceptional; it is the normal condition of the input, and the tests here assert
that each case degrades into something an operator can read rather than into a confident wrong
answer.

The recurring property across all four: **a bad input must never become an invisible one.**
Every rejection is an event with a reason. Silence is visible as silence. The system is allowed
to know less; it is not allowed to claim the same confidence while knowing less.
"""

from __future__ import annotations

import pytest

from backend.common.clock import SimClock
from backend.events.store import SqliteEventStore
from backend.events.types import EventType
from backend.ingestion.pipeline import IngestionPipeline
from backend.tracking.models import TrackState
from backend.tracking.track_manager.manager import TrackManager
from backend.zones.geometry import SiteGeometry

from .conftest import (
    advance_quietly,
    canonical,
    establish_confirmed_track,
    events_of,
    incoming,
    track_by_id,
)

# ---------------------------------------------------------------------------------------
# FM-010 — a sensor goes silent
# ---------------------------------------------------------------------------------------


def test_a_silent_sensor_degrades_its_tracks_through_the_stated_lifecycle(
    tracker: TrackManager, clock: SimClock
) -> None:
    """CONFIRMED → STALE → LOST, on the schedule the configuration declares.

    The hazard FM-010 names is not that tracks are lost — it is that they are *kept*, looking
    exactly as confident as they did while real data was arriving. Each demotion below is the
    system reducing its own claim, on a timetable an operator can be told in advance.
    """
    track_id = establish_confirmed_track(tracker, clock)
    config = tracker.config

    # The sensor stops. Nothing else changes.
    advance_quietly(tracker, clock, seconds=config.coast_seconds + 1.0)
    coasted = track_by_id(tracker, track_id)
    assert coasted.state is TrackState.STALE

    advance_quietly(tracker, clock, seconds=config.stale_seconds + 1.0)
    assert track_by_id(tracker, track_id) is None, "a track past its stale window is still active"


def test_a_coasting_track_never_looks_freshly_observed(
    tracker: TrackManager, clock: SimClock
) -> None:
    """The precise deception FM-010 is about, isolated.

    A coasting track keeps being *updated* — the filter predicts it forward every cycle, so its
    position changes and its `updated_at` advances. If `last_seen` advanced with it, a track
    running on pure prediction would be indistinguishable on the wire from one a sensor is
    still painting. The two timestamps are what let the interface say "coasting" instead of
    drawing a confident position over nothing.
    """
    track_id = establish_confirmed_track(tracker, clock)
    observed_at = track_by_id(tracker, track_id).tracking.last_seen

    advance_quietly(tracker, clock, seconds=3.0)

    coasting = track_by_id(tracker, track_id)
    assert coasting.tracking.last_seen == observed_at, "prediction advanced the observation time"
    assert coasting.updated_at > observed_at
    assert coasting.tracking.is_coasting
    assert coasting.tracking.missed_updates > 0


def test_one_silent_sensor_does_not_degrade_a_track_another_sensor_still_sees(
    tracker: TrackManager, clock: SimClock
) -> None:
    """A partial outage must stay partial.

    The failure this guards against is a shared cycle-level notion of "did we get data", which
    would let one dead sensor mark every track coasting — including the ones another sensor is
    painting perfectly well.
    """
    track_id = establish_confirmed_track(tracker, clock, sensor_id="radar_north")

    for cycle in range(4):
        clock.advance(1.0)
        tracker.process(
            [
                incoming(
                    f"det-west-{cycle}",
                    at=clock.now(),
                    east_m=0.0,
                    north_m=1900.0 - 20.0 * cycle,
                    sensor_id="radar_west",
                )
            ]
        )

    still_seen = track_by_id(tracker, track_id)
    assert still_seen.state is TrackState.CONFIRMED
    assert not still_seen.tracking.is_coasting


# ---------------------------------------------------------------------------------------
# FM-011 — malformed detection data
# ---------------------------------------------------------------------------------------


def test_a_malformed_detection_is_refused_at_the_schema_boundary(
    geometry: SiteGeometry,
) -> None:
    """Rejected before it is a `Detection` at all.

    `extra="forbid"` and the field constraints mean a malformed payload cannot be constructed
    into the canonical type, so there is no code path by which one reaches ingestion. This is
    the structural half of FM-011: not "ingestion checks carefully" but "the shape does not
    exist to be passed on".
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        canonical("det-bad", geometry=geometry, confidence=42.0)  # confidence is a probability

    with pytest.raises(ValidationError):
        canonical("det-bad", geometry=geometry, uncertainty_m=-1.0)


def test_one_sensor_sending_rubbish_does_not_stop_the_others(
    ingestion: IngestionPipeline, events: SqliteEventStore, geometry: SiteGeometry, clock: SimClock
) -> None:
    """The requirement FM-011 states explicitly, and the one most easily lost.

    A batch containing a bad detection must not fail as a batch. The good detections in the
    same cycle are from *different physical devices* and are perfectly valid; discarding them
    because a neighbour misbehaved converts one broken sensor into a site-wide outage.
    """
    from datetime import timedelta

    good_before = canonical(
        "det-good-1", geometry=geometry, at=clock.now(), sensor_id="radar_north"
    )
    bad = canonical(
        "det-bad",
        geometry=geometry,
        at=clock.now() + timedelta(hours=3),  # a clock this far out is not a late arrival
        sensor_id="radar_broken",
    )
    good_after = canonical("det-good-2", geometry=geometry, at=clock.now(), sensor_id="radar_west")

    result = ingestion.ingest([good_before, bad, good_after])

    assert result.accepted == 2
    assert result.rejected == 1
    surviving = {d.detection_id for d in result.for_tracking}
    assert surviving == {"det-good-1", "det-good-2"}

    rejections = events_of(events, EventType.DETECTION_REJECTED)
    assert len(rejections) == 1
    assert rejections[0].payload["sensor_id"] == "radar_broken"
    assert rejections[0].payload["reason"], "a rejection was recorded without a reason"


# ---------------------------------------------------------------------------------------
# FM-012 — clock skew between sensors
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset_s", "expected_reason"),
    [(3600.0, "timestamp_in_future"), (-3600.0, "timestamp_too_old")],
)
def test_a_detection_from_a_badly_skewed_clock_is_refused_with_the_direction_recorded(
    ingestion: IngestionPipeline,
    events: SqliteEventStore,
    geometry: SiteGeometry,
    clock: SimClock,
    offset_s: float,
    expected_reason: str,
) -> None:
    """Both directions, and they are recorded distinctly.

    The distinction is diagnostic rather than decorative: a detection from the future can only
    be a clock problem, while one from the past could be a clock problem *or* a slow link, and
    an engineer reading the log a week later needs to be able to tell which they are chasing.
    """
    from datetime import timedelta

    ingestion.ingest(
        [canonical("det-skewed", geometry=geometry, at=clock.now() + timedelta(seconds=offset_s))]
    )

    rejections = events_of(events, EventType.DETECTION_REJECTED)
    assert len(rejections) == 1
    assert rejections[0].payload["reason"] == expected_reason


def test_skew_within_the_bound_is_accepted_rather_than_being_quietly_snapped_to_now(
    ingestion: IngestionPipeline, geometry: SiteGeometry, clock: SimClock
) -> None:
    """Tolerable skew must survive as the sensor reported it.

    Rewriting a detection's timestamp to the receive time would hide the skew and, worse, feed
    the filter a dt that never happened — the exact corruption of the velocity estimate FM-012
    is about. The system tolerates the offset; it does not pretend the offset is zero.
    """
    from datetime import timedelta

    reported = clock.now() - timedelta(seconds=5.0)
    result = ingestion.ingest([canonical("det-slightly-late", geometry=geometry, at=reported)])

    assert result.accepted == 1
    assert result.for_tracking[0].timestamp == reported


# ---------------------------------------------------------------------------------------
# FM-013 — duplicate detections
# ---------------------------------------------------------------------------------------


def test_a_repeated_detection_id_is_counted_once_however_often_it_arrives(
    ingestion: IngestionPipeline, events: SqliteEventStore, geometry: SiteGeometry
) -> None:
    """The filter must not be told the same thing twice.

    A duplicate that reached the filter would be treated as a second, independent observation
    agreeing exactly with the first — which is the strongest possible evidence, and entirely
    fictitious. The estimate would tighten around a position that has only ever been measured
    once. Asserted across a retransmission storm rather than a single repeat, because the
    interesting case is a link that is repeating everything, not one stray packet.
    """
    original = canonical("det-1", geometry=geometry)

    first = ingestion.ingest([original])
    assert first.accepted == 1

    replayed = ingestion.ingest([original] * 25)
    assert replayed.accepted == 0
    assert replayed.duplicates == 25
    assert replayed.for_tracking == []

    received = events_of(events, EventType.DETECTION_RECEIVED)
    assert len(received) == 1, "a duplicate reached the log as though it were a new observation"


def test_a_duplicate_does_not_consume_the_slot_of_a_genuine_detection_beside_it(
    ingestion: IngestionPipeline, geometry: SiteGeometry
) -> None:
    """Deduplication is per-detection, not per-batch."""
    original = canonical("det-1", geometry=geometry)
    ingestion.ingest([original])

    mixed = ingestion.ingest([original, canonical("det-2", geometry=geometry)])

    assert mixed.duplicates == 1
    assert mixed.accepted == 1
    assert [d.detection_id for d in mixed.for_tracking] == ["det-2"]


def test_tracking_has_no_duplicate_suppression_of_its_own(
    tracker: TrackManager, clock: SimClock
) -> None:
    """Ingestion's dedupe is the only thing standing between a repeat and the filter.

    This asserts the *absence* of a second safety net, which is worth pinning down precisely
    because it is invisible. Feeding one detection id repeatedly straight to the tracker raises
    the track's support count exactly as a stream of genuine observations would — the estimate
    tightens around a position that has only ever been measured once.

    So the boundary test above is load-bearing rather than belt-and-braces: if deduplication is
    ever moved, weakened, or made conditional, nothing downstream will catch what gets through.
    Should tracking ever grow its own suppression, this test should fail and be deleted
    deliberately, rather than the redundancy being assumed to have existed all along.
    """
    clock.advance(1.0)
    tracker.process([incoming("det-1", at=clock.now(), east_m=0.0, north_m=2000.0)])
    supported_once = track_by_id(tracker, "TRK-00001").tracking.detection_count

    for _ in range(5):
        clock.advance(1.0)
        tracker.process([incoming("det-1", at=clock.now(), east_m=0.0, north_m=2000.0)])

    assert track_by_id(tracker, "TRK-00001").tracking.detection_count > supported_once
