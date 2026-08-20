"""The real-radar adapter, driven from captured bytes rather than from hardware.

A hardware integration nobody can test without the hardware attached stops being tested, so
the adapter reads a `ByteTransport` and every test here supplies one. What is asserted is the
part that is dangerous to get wrong and impossible to notice on a bench: where a detection is
placed on the Earth, how much uncertainty is admitted, and what the adapter says about itself
when the link goes quiet.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

import pytest

from backend.common.clock import Clock, SimClock
from backend.common.geodesy import EnuPoint, GeodeticPoint, LocalFrame
from sensors.adapters.interface import SensorAdapterError, SensorState
from sensors.adapters.radar.adapter import UartRadarAdapter
from sensors.adapters.radar.config import (
    LinkConfig,
    ProtocolConfig,
    QualityMapping,
    RadarCoverage,
    RadarHardwareConfig,
    RadarUncertainty,
)
from sensors.adapters.radar.transport import ReplayTransport, TransportError
from sensors.schemas.detection import Provenance, SensorType
from tests.unit.test_radar_protocol import target_frame

ORIGIN = GeodeticPoint(latitude_deg=51.8971, longitude_deg=-8.4697, altitude_m=5.0)
START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_config(**overrides) -> RadarHardwareConfig:
    base = {
        "registry_key": "bench_radar_uart",
        "sensor_type": SensorType.RADAR,
        "description": "test fixture",
        "link": LinkConfig(port="/dev/null", silence_timeout_s=5.0),
        "protocol": ProtocolConfig(),
        "coverage": RadarCoverage(
            max_range_m=100.0, min_range_m=0.0, azimuth_extent_deg=40.0,
            elevation_beamwidth_deg=20.0,
        ),
        "uncertainty": RadarUncertainty(
            range_sigma_m=2.0, azimuth_sigma_deg=4.0, radial_velocity_sigma_mps=1.0
        ),
        "quality": QualityMapping(),
    }
    return RadarHardwareConfig(**(base | overrides))


def make_adapter(
    chunks: list[bytes],
    *,
    config: RadarHardwareConfig | None = None,
    clock: Clock | None = None,
    boresight_deg: float = 0.0,
    height_m: float = 2.0,
) -> tuple[UartRadarAdapter, ReplayTransport, LocalFrame]:
    frame = LocalFrame(ORIGIN)
    transport = ReplayTransport(chunks)
    adapter = UartRadarAdapter(
        clock=clock or SimClock(START),
        sensor_id="bench_radar",
        config=config or make_config(),
        transport=transport,
        position_enu=EnuPoint(east_m=0.0, north_m=0.0, up_m=height_m),
        frame=frame,
        boresight_deg=boresight_deg,
    )
    return adapter, transport, frame


def detections_from(adapter: UartRadarAdapter, expected: int, timeout_s: float = 3.0) -> list:
    """Poll until the reader thread has handed over `expected` detections, or give up.

    The reader is a real thread reading a real (if fake) transport, so the hand-off is
    genuinely asynchronous and a test that polled once would be flaky rather than fast.
    """
    collected: list = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        collected.extend(adapter.get_detections())
        if len(collected) >= expected:
            return collected
        time.sleep(0.01)
    return collected


def test_polling_before_start_raises_rather_than_reporting_an_empty_sky() -> None:
    adapter, _, _ = make_adapter([])

    with pytest.raises(SensorAdapterError, match="polled before start"):
        adapter.get_detections()


def test_a_target_on_boresight_lands_where_the_boresight_points() -> None:
    """Bearing, not just distance. A sign error here puts tracks on the wrong side of the site."""
    adapter, _, frame = make_adapter([target_frame((0x80, 60, 1, 18, 50))], boresight_deg=90.0)
    adapter.start()

    detection = detections_from(adapter, 1)[0]
    enu = frame.to_enu(
        GeodeticPoint(
            latitude_deg=detection.position.latitude,
            longitude_deg=detection.position.longitude,
            altitude_m=detection.position.altitude_m,
        )
    )
    adapter.stop()

    assert enu.east_m == pytest.approx(60.0, abs=0.5)
    assert enu.north_m == pytest.approx(0.0, abs=0.5)


def test_azimuth_sign_decides_which_side_of_boresight_a_target_appears() -> None:
    """Unverified on this device, so it is configuration — and configuration under test."""
    raw = [target_frame((0x8A, 50, 1, 18, 50))]

    right, _, frame = make_adapter(raw)
    left, _, _ = make_adapter(raw, config=make_config(protocol=ProtocolConfig(azimuth_sign=-1)))
    right.start()
    left.start()

    east_of = lambda d: frame.to_enu(  # noqa: E731 - one expression, used twice
        GeodeticPoint(
            latitude_deg=d.position.latitude,
            longitude_deg=d.position.longitude,
            altitude_m=d.position.altitude_m,
        )
    ).east_m
    east_right = east_of(detections_from(right, 1)[0])
    east_left = east_of(detections_from(left, 1)[0])
    right.stop()
    left.stop()

    assert east_right > 0.0
    assert east_left == pytest.approx(-east_right, abs=0.5)


def test_mount_tilt_is_the_only_thing_that_lifts_a_detection_off_the_mount_height() -> None:
    """The device reports no elevation; a tilted housing is the only vertical information."""
    level, _, _ = make_adapter([target_frame((0x80, 100, 1, 0, 50))], height_m=2.0)
    tilted, _, _ = make_adapter(
        [target_frame((0x80, 100, 1, 0, 50))], config=make_config(tilt_deg=30.0), height_m=2.0
    )
    level.start()
    tilted.start()

    flat = detections_from(level, 1)[0]
    raised = detections_from(tilted, 1)[0]
    level.stop()
    tilted.stop()

    assert flat.position.altitude_m == pytest.approx(ORIGIN.altitude_m + 2.0, abs=0.5)
    assert raised.position.altitude_m == pytest.approx(ORIGIN.altitude_m + 2.0 + 50.0, abs=0.5)


def test_the_unmeasured_vertical_is_admitted_in_the_covariance() -> None:
    """Elevation is not measured at all, and the covariance has to say so.

    A vertical sigma comparable to the range sigma would tell the filter this device knows an
    altitude it has no way of knowing (FM-004).
    """
    adapter, _, _ = make_adapter([target_frame((0x80, 100, 1, 0, 50))])
    adapter.start()

    covariance = detections_from(adapter, 1)[0].measurement.position_covariance_enu
    adapter.stop()

    assert covariance is not None
    vertical_sigma = math.sqrt(covariance[2][2])
    horizontal_sigma = math.sqrt(max(covariance[0][0], covariance[1][1]))
    assert vertical_sigma > horizontal_sigma
    assert vertical_sigma == pytest.approx(100.0 * math.radians(20.0) / math.sqrt(12.0), rel=1e-6)


def test_cross_range_uncertainty_grows_with_range_and_range_uncertainty_does_not() -> None:
    """Bearing error subtends more metres the further away the target is. The filter needs that."""
    near, _, _ = make_adapter([target_frame((0x80, 10, 1, 0, 50))])
    far, _, _ = make_adapter([target_frame((0x80, 100, 1, 0, 50))])
    near.start()
    far.start()

    near_cov = detections_from(near, 1)[0].measurement.position_covariance_enu
    far_cov = detections_from(far, 1)[0].measurement.position_covariance_enu
    near.stop()
    far.stop()

    assert near_cov is not None and far_cov is not None
    # On boresight (north), across-range is the east axis and along-range is the north axis.
    assert far_cov[0][0] == pytest.approx(near_cov[0][0] * 100.0, rel=1e-6)
    assert far_cov[1][1] == pytest.approx(near_cov[1][1], rel=1e-6)


def test_a_detection_carries_measured_range_rate_and_no_classification() -> None:
    """The device detects motion; it does not identify what is moving, and must not imply it."""
    adapter, _, _ = make_adapter([target_frame((0x80, 40, 1, 36, 50))])
    adapter.start()

    detection = detections_from(adapter, 1)[0]
    adapter.stop()

    assert detection.classification is None
    assert detection.kinematics is not None
    assert detection.kinematics.provenance is Provenance.MEASURED
    assert detection.kinematics.radial_velocity_mps == pytest.approx(-10.0)
    assert detection.kinematics.speed_mps is None
    assert detection.kinematics.heading_deg is None


def test_the_unitless_signal_level_is_never_reported_as_decibels() -> None:
    """`snr_db` is named in a unit this device does not report in."""
    adapter, _, _ = make_adapter([target_frame((0x80, 40, 1, 36, 90))])
    adapter.start()

    detection = detections_from(adapter, 1)[0]
    adapter.stop()

    assert detection.quality.snr_db is None
    assert detection.quality.confidence == pytest.approx(0.9)


def test_a_detection_declares_itself_real_rather_than_simulated() -> None:
    """The one channel that separates real from synthetic on the operator's display."""
    adapter, _, _ = make_adapter([target_frame((0x80, 40, 1, 36, 50))])
    adapter.start()

    detection = detections_from(adapter, 1)[0]
    adapter.stop()

    assert detection.sensor.sensor_type is SensorType.RADAR
    assert not detection.sensor.sensor_type.is_simulated


def test_reports_outside_the_configured_coverage_are_not_admitted() -> None:
    """A report from beyond the configured range is a decode error, not a distant target."""
    adapter, _, _ = make_adapter(
        [target_frame((0x80, 200, 1, 10, 50), (0xB4, 40, 1, 10, 50), (0x80, 40, 1, 10, 50))]
    )
    adapter.start()

    detections = detections_from(adapter, 1)
    health = adapter.health()
    adapter.stop()

    assert len(detections) == 1
    assert health.state is SensorState.OK
    assert "rejected" in health.reason


def test_an_empty_report_leaves_the_sensor_healthy_and_silent() -> None:
    """Frames with no targets are how a working device says the sky is empty (FM-010)."""
    adapter, _, _ = make_adapter([target_frame(alarm=0)])
    adapter.start()
    time.sleep(0.05)

    detections = adapter.get_detections()
    health = adapter.health()
    adapter.stop()

    assert detections == []
    assert health.state is SensorState.OK
    assert health.detections_emitted == 0


def test_a_link_delivering_nothing_at_all_is_degraded_rather_than_quiet() -> None:
    """Silence on the wire is the link failing, not the sky being empty. The two must differ."""
    clock = SimClock(START)
    adapter, _, _ = make_adapter([], clock=clock)
    adapter.start()

    assert adapter.health().state is SensorState.OK
    clock.advance(30.0)
    health = adapter.health()
    adapter.stop()

    assert health.state is SensorState.DEGRADED
    assert "no data" in health.reason


def test_a_failing_link_reports_offline_and_says_why() -> None:
    class FailingTransport:
        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

        def read(self) -> bytes:
            raise TransportError("device disconnected")

    frame = LocalFrame(ORIGIN)
    adapter = UartRadarAdapter(
        clock=SimClock(START),
        sensor_id="bench_radar",
        config=make_config(),
        transport=FailingTransport(),
        position_enu=EnuPoint(east_m=0.0, north_m=0.0, up_m=2.0),
        frame=frame,
    )
    adapter.start()

    deadline = time.monotonic() + 2.0
    while adapter.health().state is not SensorState.OFFLINE and time.monotonic() < deadline:
        time.sleep(0.01)
    health = adapter.health()
    adapter.stop()

    assert health.state is SensorState.OFFLINE
    assert "device disconnected" in health.reason


def test_a_port_that_will_not_open_refuses_to_start() -> None:
    """Starting anyway would present an unopened port as a sensor watching an empty sky."""

    class UnopenableTransport:
        def open(self) -> None:
            raise TransportError("no such device")

        def close(self) -> None:
            return None

        def read(self) -> bytes:  # pragma: no cover - never reached
            return b""

    adapter = UartRadarAdapter(
        clock=SimClock(START),
        sensor_id="bench_radar",
        config=make_config(),
        transport=UnopenableTransport(),
        position_enu=EnuPoint(east_m=0.0, north_m=0.0, up_m=2.0),
        frame=LocalFrame(ORIGIN),
    )

    with pytest.raises(TransportError, match="no such device"):
        adapter.start()
    assert adapter.health().state is SensorState.NOT_STARTED


def test_a_stopped_adapter_reports_nothing_and_says_it_stopped() -> None:
    adapter, transport, _ = make_adapter([target_frame((0x80, 40, 1, 10, 50))])
    adapter.start()
    detections_from(adapter, 1)
    adapter.stop()

    assert adapter.get_detections() == []
    assert adapter.health().state is SensorState.OFFLINE
    assert transport._closed is True
