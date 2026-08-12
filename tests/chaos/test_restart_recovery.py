"""FM-030 — the backend restarts while tracks are live.

A restart is the most ordinary failure a deployed system has: a deploy, an OOM kill, a power
event, an operator restarting a service they think is wedged. What makes it worth a chaos test
rather than a unit test is that nothing *inside* a cycle can detect it. Every symptom appears
across the boundary between two processes, which is precisely the seam a single-process test
suite never crosses.

These tests therefore build a real `Pipeline` against a real on-disk store, throw it away, and
build a second one over the same directory — the actual restart, not a simulation of one.

The property under test is not "state survives". It largely does not, and ADR 0017 says so.
The property is that **what does not survive is closed out honestly**, because the dangerous
failure here is not a missing track. It is a track that stays on the display, at the position
and confidence it held the instant the process died, over a piece of sky nothing is watching.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from backend.common.clock import SimClock
from backend.common.identifiers import track_id_sequence
from backend.events.types import EventType
from backend.main import Pipeline
from backend.tracking.models import TrackState
from backend.tracking.track_manager.manager import IncomingDetection
from backend.zones.geometry import SiteGeometry, load_site_geometry

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def geometry() -> SiteGeometry:
    return load_site_geometry("fictional_test_range")


def detection(sequence: int, at: datetime, *, north_m: float) -> IncomingDetection:
    return IncomingDetection(
        detection_id=f"det-{sequence:04d}",
        sensor_id="radar_north",
        timestamp=at,
        position_enu=np.array([0.0, north_m, 120.0]),
        covariance=np.eye(3) * 225.0,
    )


def run_until_confirmed(pipeline: Pipeline, clock: SimClock) -> str:
    """Drive one real track to CONFIRMED through the whole pipeline, and return its id."""
    for cycle in range(6):
        clock.advance(1.0)
        ingested = [detection(cycle, clock.now(), north_m=2000.0 - 20.0 * cycle)]
        tracks = pipeline.tracker.process(ingested)
        pipeline.projection.catch_up()

    confirmed = [t for t in tracks if t.state is TrackState.CONFIRMED]
    assert len(confirmed) == 1, [(t.track_id, str(t.state)) for t in tracks]
    return confirmed[0].track_id


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "aetos-data"


def build(data_dir: Path, geometry: SiteGeometry, clock: SimClock) -> Iterator[Pipeline]:
    pipeline = Pipeline(clock, geometry, data_dir)
    return pipeline


def test_a_track_left_live_by_a_dead_process_does_not_stay_on_the_display(
    data_dir: Path, geometry: SiteGeometry
) -> None:
    """The FM-030 hazard in its worst form.

    Before this was handled, `GET /tracks` read the projection, the projection was only ever
    written by a tracker, and the tracker that owned this track no longer existed — so nothing
    in the system was capable of ever moving it out of CONFIRMED. It sat there permanently:
    a confident position, with a plausible-looking timestamp, over empty sky.
    """
    first_clock = SimClock(START)
    first = Pipeline(first_clock, geometry, data_dir)
    track_id = run_until_confirmed(first, first_clock)
    before = {
        projected.track.track_id: projected.track
        for projected in first.repositories.tracks.list_all()
    }
    first.close()

    # The precondition, asserted rather than assumed: the dead process really did leave this
    # track live. Without this the test would still pass against a build that never created
    # the track at all, which is the failure mode a restart test is most likely to hide.
    assert before[track_id].state is TrackState.CONFIRMED

    # A second process over the same data directory: the restart.
    second = Pipeline(SimClock(START), geometry, data_dir)
    try:
        after = {
            projected.track.track_id: projected.track
            for projected in second.repositories.tracks.list_all()
        }
        assert after[track_id].state is TrackState.LOST, (
            "a track the previous process left CONFIRMED is still being served as CONFIRMED; "
            "nothing in this process can ever retire it"
        )
    finally:
        second.close()


def test_the_retirement_says_why_rather_than_looking_like_an_ordinary_track_loss(
    data_dir: Path, geometry: SiteGeometry
) -> None:
    """A restart-driven loss and an object leaving the area must be distinguishable.

    They mean opposite things to whoever reads the log afterwards: one is the system losing
    the object, the other is the object going away. Recording both as a bare `TRACK_LOST`
    would make an incident reconstruction unable to tell which happened.
    """
    first_clock = SimClock(START)
    first = Pipeline(first_clock, geometry, data_dir)
    track_id = run_until_confirmed(first, first_clock)
    first.close()

    second = Pipeline(SimClock(START), geometry, data_dir)
    try:
        losses = [
            event
            for event in second.events.read_from(1)
            if event.event_type is EventType.TRACK_LOST and event.track_id == track_id
        ]
        assert losses, "the track was not retired at all"
        assert losses[-1].payload.get("reason") == "backend_restarted"
    finally:
        second.close()


def test_identifiers_are_not_reissued_after_a_restart(
    data_dir: Path, geometry: SiteGeometry
) -> None:
    """The corrupted-audit-trail case, and the reason this is a fix rather than a tidy-up.

    `track_history` is keyed by `(track_id, seq)` and is never pruned. A second process that
    restarts its counter at TRK-00001 does not overwrite the first track's history — its rows
    interleave with them under one identifier, and two physically different objects become one
    record that cannot be separated again afterwards. Unlike a lost track, this is not
    recoverable by waiting: the evidence is gone.
    """
    first_clock = SimClock(START)
    first = Pipeline(first_clock, geometry, data_dir)
    first_id = run_until_confirmed(first, first_clock)
    first.close()

    second_clock = SimClock(START)
    second = Pipeline(second_clock, geometry, data_dir)
    try:
        second_id = run_until_confirmed(second, second_clock)
    finally:
        second.close()

    assert second_id != first_id
    assert track_id_sequence(second_id) > track_id_sequence(first_id)


def test_a_restart_with_no_prior_tracks_is_not_a_special_case(
    data_dir: Path, geometry: SiteGeometry
) -> None:
    """A first-ever start has an empty store, which must not read as "everything was lost"."""
    pipeline = Pipeline(SimClock(START), geometry, data_dir)
    try:
        losses = [
            event
            for event in pipeline.events.read_from(1)
            if event.event_type is EventType.TRACK_LOST
        ]
        assert losses == []
    finally:
        pipeline.close()
