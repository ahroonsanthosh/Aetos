"""The camera adapter: moving regions to bearing-only detections, honestly uncertain."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from backend.common.clock import SimClock
from backend.common.geodesy import GeodeticPoint, LocalFrame
from sensors.adapters.camera import CameraAdapter, CameraFrame, CameraMount, MotionRegion
from sensors.adapters.interface import SensorAdapterError, SensorState
from sensors.schemas.detection import SensorType

START = datetime(2026, 1, 1, tzinfo=UTC)
MOUNT = CameraMount(
    latitude=51.9,
    longitude=-8.47,
    altitude_m=60.0,
    boresight_deg=90.0,
    tilt_deg=0.0,
    horizontal_fov_deg=60.0,
    vertical_fov_deg=40.0,
    assumed_range_m=400.0,
    range_sigma_m=300.0,
)


def adapter(clock: SimClock | None = None) -> CameraAdapter:
    camera = CameraAdapter(clock or SimClock(START), "browser_camera", MOUNT, "s1")
    camera.start()
    return camera


def centred(width: int = 160, height: int = 90, size: float = 4.0) -> CameraFrame:
    region = MotionRegion(
        x=width / 2 - size / 2, y=height / 2 - size / 2, width=size, height=size, strength=1.0
    )
    return CameraFrame(width=width, height=height, regions=(region,))


def enu_of(detection) -> tuple[float, float, float]:
    frame = LocalFrame(GeodeticPoint(MOUNT.latitude, MOUNT.longitude, MOUNT.altitude_m))
    p = detection.position
    e = frame.to_enu(GeodeticPoint(p.latitude, p.longitude, p.altitude_m))
    return e.east_m, e.north_m, e.up_m


def test_a_region_at_the_image_centre_lies_on_the_boresight_at_the_assumed_range() -> None:
    camera = adapter()
    camera.submit(centred())
    (detection,) = camera.get_detections()

    east, north, up = enu_of(detection)
    assert east == pytest.approx(400.0, abs=0.5)
    assert north == pytest.approx(0.0, abs=0.5)
    assert up == pytest.approx(0.0, abs=0.5)
    assert detection.sensor.sensor_type is SensorType.EO_IR
    assert detection.kinematics is None, "a camera measures no speed"


def test_the_frame_edge_maps_to_half_the_field_of_view() -> None:
    camera = adapter()
    edge = MotionRegion(x=158, y=43, width=2, height=4, strength=1.0)
    camera.submit(CameraFrame(width=160, height=90, regions=(edge,)))
    (detection,) = camera.get_detections()
    east, north, _ = enu_of(detection)
    bearing = math.degrees(math.atan2(east, north)) % 360
    # 90 + almost 30: right edge of a 60-degree view pointing east.
    assert bearing == pytest.approx(119.6, abs=0.3)


def test_uncertainty_is_large_along_the_line_of_sight_and_small_across_it() -> None:
    camera = adapter()
    camera.submit(centred())
    (detection,) = camera.get_detections()
    cov = np.array(detection.measurement.position_covariance_enu)

    # Pointing east, so along-range is the east axis.
    assert math.sqrt(cov[0, 0]) == pytest.approx(300.0, rel=0.01)
    assert math.sqrt(cov[1, 1]) < 10.0
    assert detection.measurement.position_uncertainty_m >= math.sqrt(cov[0, 0])
    assert np.all(np.linalg.eigvalsh(cov) > 0)


def test_frames_are_stamped_on_arrival_and_drained_in_order() -> None:
    clock = SimClock(START)
    camera = adapter(clock)
    camera.submit(centred())
    clock.advance(0.25)
    camera.submit(centred())
    first, second = camera.get_detections()
    assert first.timestamp == START
    assert (second.timestamp - first.timestamp).total_seconds() == pytest.approx(0.25)
    assert first.detection_id != second.detection_id
    assert camera.get_detections() == []


def test_a_camera_that_stops_sending_says_so() -> None:
    clock = SimClock(START)
    camera = adapter(clock)
    assert camera.health().state is SensorState.OK
    clock.advance(10)
    health = camera.health()
    assert health.state is SensorState.DEGRADED
    assert "no frame" in health.reason


def test_submitting_to_a_stopped_camera_is_refused() -> None:
    camera = adapter()
    camera.stop()
    with pytest.raises(SensorAdapterError):
        camera.submit(centred())
    assert camera.health().state is SensorState.OFFLINE


def test_polling_before_start_is_a_wiring_error() -> None:
    camera = CameraAdapter(SimClock(START), "browser_camera", MOUNT, "s1")
    with pytest.raises(SensorAdapterError):
        camera.get_detections()


def test_an_implausibly_small_range_uncertainty_is_still_allowed_but_a_huge_one_is_not() -> None:
    with pytest.raises(ValueError):
        MOUNT.model_validate({**MOUNT.model_dump(), "range_sigma_m": 900.0})
