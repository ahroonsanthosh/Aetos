"""Fixtures shared by the sensor ingestion tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from backend.common.geodesy import GeodeticPoint, LocalFrame
from sensors.config.loader import SensorConfig, load_sensor_config


@pytest.fixture
def config() -> SensorConfig:
    """The checked-in configuration, loaded and validated.

    Tests read the real document rather than a fixture of their own so that a change which
    invalidates it — a renamed key, a threshold outside its bounds — fails here rather than on
    the bench.
    """
    return load_sensor_config()


@pytest.fixture
def clock() -> SimClock:
    """Manually advanced time. Nothing in these tests sleeps."""
    return SimClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
def frame(config: SensorConfig) -> LocalFrame:
    """The site's local ENU frame, anchored at the configured origin."""
    return LocalFrame(
        GeodeticPoint(
            config.site_origin.latitude,
            config.site_origin.longitude,
            config.site_origin.altitude_m,
        )
    )


collect_ignore = ["manual_camera_test.py"]
"""`manual_camera_test.py` matches pytest's `*_test.py` pattern but is not a test.

It needs a handset on the network and a human in front of the lens, so it is run by hand and
must never be collected into a suite that has to pass in CI.
"""
