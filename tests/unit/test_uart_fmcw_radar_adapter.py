"""The serial-attached FMCW radar adapter, driven by a fake byte link.

No hardware, no `pyserial`, no sleeping. The adapter is constructed against a `ByteLink` and
fed a scripted byte stream, which is what makes the interesting cases — a link that dies
mid-session, a stream that never syncs, a target the sensor cannot possibly have measured the
altitude of — testable at all.

The load-bearing test here is `test_altitude_uncertainty_says_the_sensor_cannot_measure_it`.
This hardware has no elevation channel, and the danger is not that it reports the wrong
altitude, it is that it reports one the tracker believes.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from backend.common.geodesy import GeodeticPoint, LocalFrame
from sensors.adapters.interface import SensorAdapterError, SensorState
from sensors.adapters.uart_fmcw_radar.adapter import UartFmcwRadarAdapter
from sensors.adapters.uart_fmcw_radar.config import (
    DetectionGateConfig,
    MountConfig,
    UartFmcwRadarConfig,
    UartLinkConfig,
)
from sensors.adapters.uart_fmcw_radar.link import LinkError
from sensors.adapters.uart_fmcw_radar.protocol import REPORT_HEADER, REPORT_TAIL
from sensors.schemas.detection import Provenance, SensorType

START = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
SITE_LAT = 51.8971
SITE_LON = -8.4697


class FakeLink:
    """A scripted byte link. `queue()` schedules what the next read returns."""

    def __init__(self) -> None:
        self._pending = bytearray()
        self.written: list[bytes] = []
        self.is_open = False
        self.open_count = 0
        self.fail_open = False
        self.fail_read = False

    def open(self) -> None:
        self.open_count += 1
        if self.fail_open:
            raise LinkError("no such device")
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def read(self, max_bytes: int) -> bytes:
        if self.fail_read:
            raise LinkError("device disappeared")
        data = bytes(self._pending[:max_bytes])
        del self._pending[:max_bytes]
        return data

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def queue(self, data: bytes) -> None:
        self._pending.extend(data)


def frame(payload: bytes) -> bytes:
    return REPORT_HEADER + len(payload).to_bytes(2, "little") + payload + REPORT_TAIL


def report(
    *,
    azimuth: int = 0x80,
    distance: int = 50,
    direction: int = 1,
    speed: int = 36,
    quality: int = 90,
) -> bytes:
    return frame(bytes((1, azimuth, distance, direction, speed, quality)))


def make_config(**overrides: object) -> UartFmcwRadarConfig:
    base: dict[str, object] = {
        "sensor_id": "radar_testbed",
        "link": UartLinkConfig(port="/dev/null", baud=115200),
        "mount": MountConfig(latitude=SITE_LAT, longitude=SITE_LON, altitude_m=5.0),
    }
    base.update(overrides)
    return UartFmcwRadarConfig(**base)  # type: ignore[arg-type]


def make_adapter(
    config: UartFmcwRadarConfig | None = None,
) -> tuple[UartFmcwRadarAdapter, FakeLink, SimClock]:
    clock = SimClock(START)
    link = FakeLink()
    adapter = UartFmcwRadarAdapter(clock, config or make_config(), link=link)
    return adapter, link, clock


# -- lifecycle -------------------------------------------------------------------------


def test_construction_opens_nothing() -> None:
    """Building the adapter set from configuration must not fail on an unplugged sensor."""
    _, link, _ = make_adapter()

    assert link.open_count == 0


def test_polling_before_start_raises() -> None:
    """An empty list here would disguise a wiring error as an empty sky."""
    adapter, _, _ = make_adapter()

    with pytest.raises(SensorAdapterError):
        adapter.get_detections()


def test_stop_releases_the_link() -> None:
    adapter, link, _ = make_adapter()
    adapter.start()

    adapter.stop()

    assert link.is_open is False


# -- emission --------------------------------------------------------------------------


def test_a_report_becomes_a_canonical_detection() -> None:
    adapter, link, clock = make_adapter()
    adapter.start()
    link.queue(report(distance=50, speed=36, direction=1))

    (detection,) = adapter.get_detections()

    assert detection.sensor.sensor_type is SensorType.RADAR
    assert detection.sensor.sensor_id == "radar_testbed"
    assert detection.timestamp == clock.now()
    assert detection.kinematics is not None
    assert detection.kinematics.radial_velocity_mps == pytest.approx(-10.0)
    assert detection.kinematics.provenance is Provenance.MEASURED


def test_no_classification_hint_is_ever_emitted() -> None:
    """The firmware is vehicle-tuned. It has no basis for a claim about a small airframe, so
    the adapter makes none rather than handing the classifier evidence it invented (FM-052)."""
    adapter, link, _ = make_adapter()
    adapter.start()
    link.queue(report())

    (detection,) = adapter.get_detections()

    assert detection.classification is None


def test_the_uncalibrated_quality_byte_is_never_reported_as_decibels() -> None:
    adapter, link, _ = make_adapter()
    adapter.start()
    link.queue(report(quality=200))

    (detection,) = adapter.get_detections()

    assert detection.quality.snr_db is None
    assert detection.quality.confidence <= 0.6, "confidence stays capped until test data exists"


def test_confidence_rises_monotonically_with_the_quality_byte() -> None:
    adapter, link, _ = make_adapter()
    adapter.start()
    link.queue(report(quality=0) + report(quality=60) + report(quality=255))

    low, middle, high = adapter.get_detections()

    assert low.quality.confidence < middle.quality.confidence < high.quality.confidence


def test_geometry_places_the_target_along_the_boresight_bearing() -> None:
    """Due east at 100 m, on a boresight of 90°, with the beam level."""
    config = make_config(
        mount=MountConfig(latitude=SITE_LAT, longitude=SITE_LON, altitude_m=5.0, boresight_deg=90.0)
    )
    adapter, link, _ = make_adapter(config)
    adapter.start()
    link.queue(report(azimuth=0x80, distance=100))

    (detection,) = adapter.get_detections()

    frame_ = LocalFrame(GeodeticPoint(SITE_LAT, SITE_LON, 5.0))
    enu = frame_.to_enu(
        GeodeticPoint(
            detection.position.latitude, detection.position.longitude, detection.position.altitude_m
        )
    )
    assert enu.east_m == pytest.approx(100.0, abs=0.5)
    assert enu.north_m == pytest.approx(0.0, abs=0.5)
    assert enu.up_m == pytest.approx(0.0, abs=0.5)


def test_mount_tilt_raises_the_reported_altitude() -> None:
    """Tilt is the only elevation this hardware has, so it must reach the emitted position."""
    config = make_config(
        mount=MountConfig(latitude=SITE_LAT, longitude=SITE_LON, altitude_m=5.0, tilt_deg=20.0)
    )
    adapter, link, _ = make_adapter(config)
    adapter.start()
    link.queue(report(distance=100))

    (detection,) = adapter.get_detections()

    expected_up = 5.0 + 100.0 * math.sin(math.radians(20.0))
    assert detection.position.altitude_m == pytest.approx(expected_up, abs=1.0)


def test_altitude_uncertainty_says_the_sensor_cannot_measure_it() -> None:
    """The vertical variance must dwarf the horizontal, because elevation is not measured.

    If this ever fails, the tracker is being told to believe an altitude that came from a
    configured mount angle rather than from a measurement (FM-051).
    """
    adapter, link, _ = make_adapter()
    adapter.start()
    link.queue(report(distance=100))

    (detection,) = adapter.get_detections()

    covariance = detection.measurement.position_covariance_enu
    assert covariance is not None
    var_e, var_n, var_u = covariance[0][0], covariance[1][1], covariance[2][2]
    assert var_u > 5.0 * max(var_e, var_n), (
        "vertical variance must dominate: it is the width of a beam nobody has measured "
        "within, not a measurement error"
    )


def test_cross_range_uncertainty_grows_with_range() -> None:
    """Angular error is an angle: at 100 m it is worth five times what it is at 20 m."""
    adapter, link, _ = make_adapter()
    adapter.start()
    link.queue(report(distance=20) + report(distance=100))

    near, far = adapter.get_detections()

    assert far.measurement.position_uncertainty_m > near.measurement.position_uncertainty_m


# -- gating ----------------------------------------------------------------------------


def test_reports_outside_the_configured_range_window_are_dropped() -> None:
    gate = DetectionGateConfig(min_range_m=20.0, max_range_m=80.0)
    adapter, link, _ = make_adapter(make_config(gate=gate))
    adapter.start()
    link.queue(report(distance=5) + report(distance=50) + report(distance=200))

    detections = adapter.get_detections()

    assert len(detections) == 1


def test_reports_outside_the_physical_beam_are_dropped() -> None:
    """±20° is the whole cone. An angle outside it means the frame or the layout is wrong."""
    adapter, link, _ = make_adapter()
    adapter.start()
    link.queue(report(azimuth=0x80 + 45))

    assert adapter.get_detections() == []


def test_reports_below_the_quality_floor_are_dropped() -> None:
    adapter, link, _ = make_adapter(make_config(gate=DetectionGateConfig(min_quality=50)))
    adapter.start()
    link.queue(report(quality=10))

    assert adapter.get_detections() == []


# -- health and link failure -------------------------------------------------------------


def test_health_before_start_is_not_started() -> None:
    adapter, _, _ = make_adapter()

    assert adapter.health().state is SensorState.NOT_STARTED


def test_a_link_that_will_not_open_reports_offline_rather_than_raising() -> None:
    """An unreachable sensor is a health state. The rest of the site keeps running."""
    adapter, link, _ = make_adapter()
    link.fail_open = True

    adapter.start()

    health = adapter.health()
    assert health.state is SensorState.OFFLINE
    assert "no such device" in health.reason
    assert adapter.get_detections() == []


def test_a_link_that_dies_mid_session_goes_offline_and_reconnects_after_backoff() -> None:
    adapter, link, clock = make_adapter()
    adapter.start()
    link.fail_read = True

    assert adapter.get_detections() == []
    assert adapter.health().state is SensorState.OFFLINE

    link.fail_read = False
    assert adapter.get_detections() == [], "retrying inside the backoff window is a busy loop"

    clock.advance(5.0)
    link.queue(report())
    assert len(adapter.get_detections()) == 1
    assert adapter.health().state is SensorState.OK


def test_a_stream_that_never_syncs_is_degraded_not_quiet() -> None:
    """The single most likely bring-up mistake — wrong baud, wrong logic level — must not
    look like a working sensor observing an empty sky (FM-050)."""
    adapter, link, _ = make_adapter()
    adapter.start()
    link.queue(bytes(range(256)) * 2)

    assert adapter.get_detections() == []

    health = adapter.health()
    assert health.state is SensorState.DEGRADED
    assert "baud" in health.reason


def test_prolonged_silence_on_an_open_link_is_degraded() -> None:
    adapter, link, clock = make_adapter()
    adapter.start()

    clock.advance(30.0)

    assert adapter.health().state is SensorState.DEGRADED


def test_detection_count_resets_on_restart() -> None:
    """The watchdog reasons about rate over a window; a count that survived a restart would
    describe a run that has ended."""
    adapter, link, _ = make_adapter()
    adapter.start()
    link.queue(report())
    adapter.get_detections()
    assert adapter.health().detections_emitted == 1

    adapter.stop()
    adapter.start()

    assert adapter.health().detections_emitted == 0


# -- configuration -----------------------------------------------------------------------


def test_configure_wraps_writes_in_the_handshake() -> None:
    """The unit reports nothing while in configuration mode, so it must always be left."""
    adapter, link, _ = make_adapter()
    adapter.start()

    adapter.configure(max_range_m=60, min_speed_kmh=2, sensitivity_threshold=6)

    assert len(link.written) == 4
    assert link.written[0][6:8] == (0x00FF).to_bytes(2, "little"), "enable config first"
    assert link.written[-1][6:8] == (0x00FE).to_bytes(2, "little"), "end config last"


def test_configuration_mode_is_left_even_when_a_write_fails() -> None:
    adapter, link, _ = make_adapter()
    adapter.start()

    class Failing(FakeLink):
        def write(self, data: bytes) -> None:
            self.written.append(data)
            if len(self.written) == 2:
                raise LinkError("write failed")

    failing = Failing()
    failing.open()
    adapter._link = failing  # noqa: SLF001 — exercising the failure path needs the seam

    with pytest.raises(SensorAdapterError):
        adapter.configure(max_range_m=60)

    assert failing.written[-1][6:8] == (0x00FE).to_bytes(2, "little")


def test_configure_on_a_closed_link_raises_rather_than_pretending() -> None:
    """Silently not applying a sensitivity change produces a session nobody can interpret."""
    adapter, link, _ = make_adapter()
    link.fail_open = True
    adapter.start()

    with pytest.raises(SensorAdapterError):
        adapter.configure(sensitivity_threshold=6)


def test_out_of_range_command_values_are_rejected_not_truncated() -> None:
    adapter, _, _ = make_adapter()
    adapter.start()

    with pytest.raises(SensorAdapterError):
        adapter.configure(max_range_m=400)


# -- raw logging -------------------------------------------------------------------------


def test_raw_bytes_are_logged_before_they_are_interpreted(tmp_path) -> None:
    """Nothing this hardware reports is validated yet, and the raw log is the only thing that
    can validate it after the fact."""
    log = tmp_path / "session" / "raw.log"
    adapter, link, _ = make_adapter(make_config(raw_log_path=log))
    adapter.start()
    link.queue(report(distance=50))

    adapter.get_detections()

    line = log.read_text().strip()
    assert line.endswith(report(distance=50).hex())


def test_bytes_that_decode_to_nothing_are_logged_too(tmp_path) -> None:
    """A session where the parser saw nothing is only explicable if the bytes it saw nothing
    in were kept — which is the case a raw log exists for."""
    log = tmp_path / "raw.log"
    adapter, link, _ = make_adapter(make_config(raw_log_path=log))
    adapter.start()
    noise = bytes(range(64))
    link.queue(noise)

    assert adapter.get_detections() == []

    assert log.read_text().strip().endswith(noise.hex())


def test_a_bad_log_path_does_not_stop_the_sensor_reporting(tmp_path) -> None:
    """A full disk or an unwritable path is a gap in the log, not a sensor that stopped."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    adapter, link, _ = make_adapter(make_config(raw_log_path=blocker / "raw.log"))
    adapter.start()
    link.queue(report())

    assert len(adapter.get_detections()) == 1
