"""Shared fixtures and a small builder for the chaos suite.

A chaos test drives the *system* into an adverse state and asserts on what an operator would
see afterwards. That is a different question from the one the unit suite asks, and it needs a
different harness: real components wired together, a clock the test advances by hand, and a
detection source the test can corrupt, silence, duplicate or flood at will.

Everything here is deliberately real except the clock and the detection source. The tracker,
ingestion, the event store and the site geometry are the production classes, because a
resilience claim about a stubbed tracker is a claim about the stub.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import numpy as np
import pytest

from backend.common.clock import SimClock
from backend.events.store import SqliteEventStore
from backend.ingestion.pipeline import IngestionPipeline
from backend.tracking.models import TrackState
from backend.tracking.track_manager.manager import IncomingDetection, TrackManager
from backend.zones.geometry import SiteGeometry, load_site_geometry
from sensors.schemas.detection import (
    Detection,
    GeoPosition,
    Measurement,
    Quality,
    SensorRef,
    SensorType,
)

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)

SITE_NAME = "fictional_test_range"


@pytest.fixture(scope="session")
def geometry() -> SiteGeometry:
    return load_site_geometry(SITE_NAME)


@pytest.fixture
def clock() -> SimClock:
    return SimClock(START)


@pytest.fixture
def events(clock: SimClock) -> Iterator[SqliteEventStore]:
    store = SqliteEventStore(clock)
    yield store
    store.close()


@pytest.fixture
def tracker(clock: SimClock, events: SqliteEventStore, geometry: SiteGeometry) -> TrackManager:
    return TrackManager(clock, events, geometry.frame)


@pytest.fixture
def ingestion(
    clock: SimClock, events: SqliteEventStore, geometry: SiteGeometry
) -> IngestionPipeline:
    return IngestionPipeline(clock, events, geometry)


def incoming(
    detection_id: str,
    *,
    at: datetime,
    east_m: float,
    north_m: float,
    up_m: float = 120.0,
    sensor_id: str = "radar_north",
    uncertainty_m: float = 15.0,
) -> IncomingDetection:
    """A tracker input at a chosen point in the local frame.

    Positioned in ENU directly rather than in latitude and longitude: these tests are about
    what the tracker does under duress, and routing every position through the geodesy would
    make an arithmetic slip in the test look like a tracking failure.
    """
    return IncomingDetection(
        detection_id=detection_id,
        sensor_id=sensor_id,
        timestamp=at,
        position_enu=np.array([east_m, north_m, up_m]),
        covariance=np.eye(3) * (uncertainty_m**2),
    )


def canonical(
    detection_id: str,
    *,
    geometry: SiteGeometry,
    at: datetime = START,
    sensor_id: str = "radar_north",
    latitude_offset: float = 0.01,
    longitude_offset: float = 0.0,
    altitude_m: float = 120.0,
    confidence: float = 0.87,
    uncertainty_m: float = 15.0,
) -> Detection:
    """A full canonical Detection, for tests that exercise the ingestion boundary itself."""
    reference = geometry.site.reference
    return Detection(
        detection_id=detection_id,
        timestamp=at,
        sensor=SensorRef(sensor_id=sensor_id, sensor_type=SensorType.SIMULATED_RADAR),
        position=GeoPosition(
            latitude=reference.latitude + latitude_offset,
            longitude=reference.longitude + longitude_offset,
            altitude_m=altitude_m,
        ),
        measurement=Measurement(position_uncertainty_m=uncertainty_m),
        quality=Quality(confidence=confidence),
    )


def establish_confirmed_track(
    tracker: TrackManager,
    clock: SimClock,
    *,
    sensor_id: str = "radar_north",
    cycles: int = 6,
    step_s: float = 1.0,
    east_m: float = 0.0,
    north_m: float = 2000.0,
    speed_north_mps: float = -20.0,
) -> str:
    """Drive a track to CONFIRMED and return its id.

    Several tests need a track the operator is already being asked to believe before the
    adverse condition starts — a failure that only ever damages a tentative track is much less
    interesting than one that damages a confirmed one.
    """
    for cycle in range(cycles):
        clock.advance(step_s)
        tracker.process(
            [
                incoming(
                    f"det-setup-{cycle}",
                    at=clock.now(),
                    east_m=east_m,
                    north_m=north_m + speed_north_mps * cycle * step_s,
                    sensor_id=sensor_id,
                )
            ]
        )

    confirmed = [t for t in tracker.active_tracks() if t.state is TrackState.CONFIRMED]
    assert len(confirmed) == 1, (
        f"setup did not produce exactly one CONFIRMED track: "
        f"{[(t.track_id, str(t.state)) for t in tracker.active_tracks()]}"
    )
    return confirmed[0].track_id


def advance_quietly(
    tracker: TrackManager, clock: SimClock, *, seconds: float, step_s: float = 1.0
) -> None:
    """Run cycles with no detections at all — the shape of a sensor that has gone silent."""
    elapsed = 0.0
    while elapsed < seconds:
        clock.advance(step_s)
        tracker.process([])
        elapsed += step_s


def track_by_id(tracker: TrackManager, track_id: str):
    for track in tracker.active_tracks():
        if track.track_id == track_id:
            return track
    return None


def events_of(store: SqliteEventStore, event_type) -> list:
    return [event for event in store.read_from(1) if event.event_type is event_type]


