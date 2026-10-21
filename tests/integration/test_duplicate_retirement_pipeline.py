"""Duplicate retirement (ADR 0021) followed through the whole pipeline, not just the tracker.

The tracker's unit tests prove the rule. These prove what everything downstream makes of it:
the log, the projection the API serves, the live stream a console reads, and alerting — each of
which reads a retirement differently, and each of which got something wrong before it was
followed through on a real run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.alerting.service import AlertService
from backend.api.schemas import StreamedEvent
from backend.common.clock import SimClock
from backend.events.types import EventType
from backend.main import Pipeline
from backend.tracking.models import TrackState
from backend.zones.geometry import load_site_geometry

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
TICK_S = 0.5


@pytest.fixture
def retired_run(tmp_path: Path):
    """`a_benign_transit` run until the tracker retires a duplicate, then on a little further."""
    clock = SimClock(START)
    pipeline = Pipeline(clock, load_site_geometry("fictional_test_range"), tmp_path)
    pipeline.attach_scenario("a_benign_transit")
    retired: dict[str, str] = {}
    try:
        for _ in range(200):
            clock.advance(TICK_S)
            pipeline.step()
            retired = retired or pipeline.tracker.last_retired_duplicates
            if retired and pipeline.tracker.last_retired_duplicates == {}:
                break
        assert retired, "no duplicate formed; the scenario no longer exercises this path"
        for _ in range(10):
            clock.advance(TICK_S)
            pipeline.step()
        yield pipeline, retired
    finally:
        pipeline.close()


def test_the_console_is_told_which_track_carries_on(retired_run) -> None:
    """The live stream is how the console learns a track has gone. A retirement must arrive as
    the track's end *and* name its survivor, or the operator's selection vanishes with it."""
    pipeline, retired = retired_run
    (retired_id, survivor_id), *_ = retired.items()

    (lost,) = [
        e
        for e in pipeline.events.read_from(1)
        if e.event_type is EventType.TRACK_LOST and e.track_id == retired_id
    ]
    streamed = StreamedEvent.from_event(lost)
    assert streamed.detail["reason"] == "duplicate"
    assert streamed.detail["duplicate_of"] == survivor_id


def test_the_projection_shows_one_track_and_rebuilds_into_the_same(retired_run) -> None:
    """What the API serves: the duplicate ended, the survivor live — and the same again after
    replaying the log from nothing, since the survivor's combined estimate is in the log too."""
    pipeline, retired = retired_run
    (retired_id, survivor_id), *_ = retired.items()
    tracks = pipeline.repositories.tracks

    assert tracks.get(retired_id).track.state is TrackState.LOST
    assert tracks.get(survivor_id).track.state is not TrackState.LOST

    before = {p.track.track_id: p.track for p in tracks.list_all()}
    pipeline.projection.rebuild()
    after = {p.track.track_id: p.track for p in tracks.list_all()}
    assert after == before

    held = {t.track_id: t for t in pipeline.tracker.active_tracks()}
    assert retired_id not in held
    assert after[survivor_id].enu == held[survivor_id].enu


def test_alerts_rebuild_into_what_is_held(retired_run) -> None:
    """A restart after a retirement must reopen the same alerts on the same tracks — including
    one that moved to the survivor — or a restart silently changes what the operator sees."""
    pipeline, _ = retired_run
    rebuilt = AlertService(pipeline.alerting._policy, pipeline.events)
    rebuilt.rebuild()

    def key(alerts):
        return sorted((a.alert_id, a.track_id, a.state, a.peak_level) for a in alerts)

    assert key(rebuilt.active) == key(pipeline.alerting.active)
