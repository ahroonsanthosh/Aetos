"""`UartFmcwRadarAdapter` against a fake transport — no serial port, no `pyserial`.

The fake stands in for `SerialTransport`, so these tests exercise everything the adapter is
actually responsible for: framing state across polls, the `SensorAdapter` contract (start
before poll, health reporting, never raising for a dropped link), and the conversion from raw
wire fields into a canonical `Detection` with an honestly-built covariance.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from backend.common.geodesy import EnuPoint, GeodeticPoint, LocalFrame
from sensors.adapters.interface import SensorAdapterError, SensorState
from sensors.adapters.uart_fmcw_radar.adapter import UartFmcwRadarAdapter
from sensors.adapters.uart_fmcw_radar.config import RadarMountConfig, SerialLinkConfig
from sensors.adapters.uart_fmcw_radar.protocol import DATA_FRAME_FOOTER, DATA_FRAME_HEADER
from sensors.adapters.uart_fmcw_radar.transport import SerialTransportError
from sensors.schemas.detection import SensorType

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
SITE = GeodeticPoint(53.35, -6.26, 0.0)


class FakeTransport:
    """A `SerialTransport` double: no real port, fully scripted from the test."""

    def __init__(self, *, fail_open: bool = False) -> None:
        self._open = False
        self._fail_open = fail_open
        self.open_calls = 0
        self._queue: list[bytes] = []
        self._raise_on_next_read: Exception | None = None

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self.open_calls += 1
        if self._fail_open:
            raise SerialTransportError("simulated open failure")
        self._open = True

    def close(self) -> None:
        self._open = False

    def queue(self, chunk: bytes) -> None:
        self._queue.append(chunk)

    def fail_next_read(self, exc: Exception) -> None:
        self._raise_on_next_read = exc

    def read_available(self) -> bytes:
        if self._raise_on_next_read is not None:
            exc, self._raise_on_next_read = self._raise_on_next_read, None
            raise exc
        return self._queue.pop(0) if self._queue else b""


def _frame(
    *, angle_byte: int = 0x80, distance_byte: int = 10, direction_byte: int = 0x00,
    speed_byte: int = 0, signal_byte: int = 200,
) -> bytes:
    record = bytes([angle_byte, distance_byte, direction_byte, speed_byte, signal_byte])
    payload = bytes([1, 0x00]) + record
    return DATA_FRAME_HEADER + len(payload).to_bytes(2, "little") + payload + DATA_FRAME_FOOTER


@pytest.fixture
def clock() -> SimClock:
    return SimClock(START)


@pytest.fixture
def frame() -> LocalFrame:
    return LocalFrame(SITE)


def _adapter(
    clock: SimClock, frame: LocalFrame, transport: FakeTransport, **mount_overrides: object
) -> UartFmcwRadarAdapter:
    return UartFmcwRadarAdapter(
        clock=clock,
        sensor_id="testbed_radar_01",
        position_enu=EnuPoint(east_m=0.0, north_m=0.0, up_m=0.0),
        frame=frame,
        serial_config=SerialLinkConfig(port="fake", reconnect_interval_s=5.0),
        mount_config=RadarMountConfig(boresight_deg=0.0, **mount_overrides),
        transport=transport,
    )


def test_get_detections_before_start_raises(clock: SimClock, frame: LocalFrame) -> None:
    adapter = _adapter(clock, frame, FakeTransport())

    with pytest.raises(SensorAdapterError):
        adapter.get_detections()


def test_health_before_start_is_not_started(clock: SimClock, frame: LocalFrame) -> None:
    adapter = _adapter(clock, frame, FakeTransport())
    assert adapter.health().state == SensorState.NOT_STARTED


def test_health_ok_after_start_with_a_working_link(clock: SimClock, frame: LocalFrame) -> None:
    adapter = _adapter(clock, frame, FakeTransport())
    adapter.start()
    assert adapter.health().state == SensorState.OK


def test_health_offline_and_no_raise_when_the_port_cannot_open(
    clock: SimClock, frame: LocalFrame
) -> None:
    adapter = _adapter(clock, frame, FakeTransport(fail_open=True))
    adapter.start()

    assert adapter.health().state == SensorState.OFFLINE
    assert adapter.get_detections() == []


def test_stop_then_poll_returns_empty_without_raising(clock: SimClock, frame: LocalFrame) -> None:
    adapter = _adapter(clock, frame, FakeTransport())
    adapter.start()
    adapter.stop()

    assert adapter.get_detections() == []
    assert adapter.health().state == SensorState.OFFLINE


def test_a_boresight_return_produces_a_detection_with_the_right_sensor_ref(
    clock: SimClock, frame: LocalFrame
) -> None:
    transport = FakeTransport()
    adapter = _adapter(clock, frame, transport)
    adapter.start()
    transport.queue(_frame(distance_byte=20))

    detections = adapter.get_detections()

    assert len(detections) == 1
    detection = detections[0]
    assert detection.sensor.sensor_id == "testbed_radar_01"
    assert detection.sensor.sensor_type == SensorType.RADAR
    assert detection.timestamp == clock.now()
    assert detection.classification is None


def test_a_boresight_return_lands_north_of_the_sensor(clock: SimClock, frame: LocalFrame) -> None:
    """boresight_deg=0 (north), angle byte 0x80 (dead ahead), tilt 0 -> straight north."""
    transport = FakeTransport()
    adapter = _adapter(clock, frame, transport, tilt_deg=0.0)
    adapter.start()
    transport.queue(_frame(angle_byte=0x80, distance_byte=30))

    detection = adapter.get_detections()[0]
    enu = frame.to_enu(
        GeodeticPoint(
            detection.position.latitude, detection.position.longitude, detection.position.altitude_m
        )
    )

    assert enu.north_m == pytest.approx(30.0, abs=1e-6)
    assert enu.east_m == pytest.approx(0.0, abs=1e-6)


def test_a_tilted_mount_lifts_the_inferred_altitude(clock: SimClock, frame: LocalFrame) -> None:
    transport = FakeTransport()
    adapter = _adapter(clock, frame, transport, tilt_deg=30.0)
    adapter.start()
    transport.queue(_frame(distance_byte=20))

    detection = adapter.get_detections()[0]
    enu = frame.to_enu(
        GeodeticPoint(
            detection.position.latitude, detection.position.longitude, detection.position.altitude_m
        )
    )

    assert enu.up_m > 0.0


def test_default_direction_byte_zero_is_approaching_and_negative(
    clock: SimClock, frame: LocalFrame
) -> None:
    transport = FakeTransport()
    adapter = _adapter(clock, frame, transport)
    adapter.start()
    transport.queue(_frame(direction_byte=0x00, speed_byte=36))  # 36 km/h = 10 m/s

    detection = adapter.get_detections()[0]

    assert detection.kinematics is not None
    assert detection.kinematics.radial_velocity_mps == pytest.approx(-10.0)


def test_flipping_direction_polarity_via_config(clock: SimClock, frame: LocalFrame) -> None:
    transport = FakeTransport()
    adapter = _adapter(
        clock, frame, transport, direction_zero_means_approaching=False
    )
    adapter.start()
    transport.queue(_frame(direction_byte=0x00, speed_byte=36))

    detection = adapter.get_detections()[0]

    assert detection.kinematics.radial_velocity_mps == pytest.approx(10.0)


def test_returns_beyond_max_range_are_dropped(clock: SimClock, frame: LocalFrame) -> None:
    transport = FakeTransport()
    adapter = _adapter(clock, frame, transport, max_valid_range_m=50.0)
    adapter.start()
    transport.queue(_frame(distance_byte=200))

    assert adapter.get_detections() == []


def test_returns_below_the_signal_floor_are_dropped(clock: SimClock, frame: LocalFrame) -> None:
    transport = FakeTransport()
    adapter = _adapter(clock, frame, transport, min_signal_raw=50)
    adapter.start()
    transport.queue(_frame(signal_byte=10))

    assert adapter.get_detections() == []


def test_confidence_increases_with_signal_strength(clock: SimClock, frame: LocalFrame) -> None:
    transport = FakeTransport()
    adapter = _adapter(clock, frame, transport)
    adapter.start()
    transport.queue(_frame(signal_byte=10))
    weak = adapter.get_detections()[0].quality.confidence

    transport.queue(_frame(signal_byte=220))
    strong = adapter.get_detections()[0].quality.confidence

    assert strong > weak


def test_a_frame_split_across_two_polls_is_still_parsed(clock: SimClock, frame: LocalFrame) -> None:
    transport = FakeTransport()
    adapter = _adapter(clock, frame, transport)
    adapter.start()

    whole = _frame(distance_byte=15)
    transport.queue(whole[:6])
    assert adapter.get_detections() == []

    transport.queue(whole[6:])
    detections = adapter.get_detections()
    assert len(detections) == 1


def test_a_dropped_link_reports_offline_and_reconnects_after_the_backoff(
    clock: SimClock, frame: LocalFrame
) -> None:
    transport = FakeTransport()
    adapter = _adapter(clock, frame, transport)
    adapter.start()
    assert adapter.health().state == SensorState.OK

    transport.fail_next_read(SerialTransportError("cable pulled"))
    assert adapter.get_detections() == []
    assert adapter.health().state == SensorState.OFFLINE
    assert not transport.is_open

    # Within the backoff window: no reconnect attempt yet.
    opens_before = transport.open_calls
    adapter.get_detections()
    assert transport.open_calls == opens_before

    # Past the backoff window: reconnects and resumes.
    clock.advance(10.0)
    transport.queue(_frame(distance_byte=5))
    detections = adapter.get_detections()

    assert transport.open_calls == opens_before + 1
    assert len(detections) == 1
    assert adapter.health().state == SensorState.OK
