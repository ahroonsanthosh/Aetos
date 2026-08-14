"""The hardware radar adapter: a physical unit as a source of canonical Detections.

The adapter is the point at which hardware stops being hardware, so what is tested here is
mostly the adapter contract from `docs/detection_schema.md` applied to this class of sensor —
canonical frame and datum, honest uncertainty, truthful provenance, and the rule that an empty
list means an empty sky and nothing else.

A stub `RadarSensor` stands in for the driver throughout. That is not a shortcut: the adapter is
written against the interface and must never need a specific driver, and a test that reached for
one would be evidence the boundary had already leaked.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from backend.common.geodesy import EnuPoint, GeodeticPoint, LocalFrame
from sensors.adapters.hardware_radar import HardwareRadarAdapter
from sensors.adapters.interface import SensorAdapterError, SensorState
from sensors.radar.config import MountConfig, RadarConfig, SerialLinkConfig, UncertaintyConfig
from sensors.radar.radar_interface import (
    RadarConnectionState,
    RadarSensor,
    RadarSensorError,
    SensorStatus,
    TargetDetection,
)
from sensors.schemas.detection import Provenance, SensorType

EPOCH = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
ORIGIN = GeodeticPoint(latitude_deg=51.5, longitude_deg=-0.12, altitude_m=30.0)


class StubRadar(RadarSensor):
    """A `RadarSensor` under the test's control. No protocol, no bytes, no hardware."""

    def __init__(self, *, state: RadarConnectionState = RadarConnectionState.CONNECTED) -> None:
        self.connected = False
        self.state = state
        self.queued: list[TargetDetection] = []
        self.status_counters = {"frames_decoded": 1, "bytes_discarded": 0, "malformed_frames": 0}
        self.raise_on_poll: RadarSensorError | None = None

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def get_targets(self) -> list[TargetDetection]:
        if self.raise_on_poll is not None:
            raise self.raise_on_poll
        targets, self.queued = self.queued, []
        return targets

    def get_status(self) -> SensorStatus:
        return SensorStatus(
            state=self.state,
            reason="stub",
            port="/dev/null",
            baud_rate=115_200,
            last_frame_monotonic=0.0,
            targets_reported=0,
            reconnect_attempts=0,
            **self.status_counters,
        )

    def configure(self, **settings: object) -> None:
        return None


def target(
    *,
    timestamp: float = 0.0,
    range_m: float = 100.0,
    azimuth_deg: float = 0.0,
    velocity_mps: float = -5.0,
    elevation_deg: float | None = None,
) -> TargetDetection:
    return TargetDetection(
        timestamp=timestamp,
        range_m=range_m,
        azimuth_deg=azimuth_deg,
        elevation_deg=elevation_deg,
        velocity_mps=velocity_mps,
        confidence=None,
        raw_source="stub-unit",
    )


def make_config(**overrides: object) -> RadarConfig:
    base: dict[str, object] = {
        "sensor_id": "bench_radar",
        "model": "stub-unit",
        "description": "stubbed radar, no hardware",
        "link": SerialLinkConfig(port="/dev/null"),
        "mount": MountConfig(),
        "uncertainty": UncertaintyConfig(),
    }
    base.update(overrides)
    return RadarConfig.model_validate(base)


def make_adapter(
    clock: SimClock, radar: StubRadar, **config: object
) -> HardwareRadarAdapter:
    return HardwareRadarAdapter(
        clock=clock,
        sensor_id="bench_radar",
        radar=radar,
        config=make_config(**config),
        position_enu=EnuPoint(0.0, 0.0, 0.0),
        frame=LocalFrame(ORIGIN),
    )


@pytest.fixture
def clock() -> SimClock:
    return SimClock(EPOCH)


@pytest.fixture
def radar() -> StubRadar:
    return StubRadar()


class TestLifecycle:
    def test_construction_does_not_connect(self, clock: SimClock, radar: StubRadar) -> None:
        make_adapter(clock, radar)

        assert radar.connected is False

    def test_start_connects_and_stop_disconnects(self, clock: SimClock, radar: StubRadar) -> None:
        adapter = make_adapter(clock, radar)
        adapter.start()
        assert radar.connected is True

        adapter.stop()
        assert radar.connected is False

    def test_polling_before_start_raises_rather_than_reporting_an_empty_sky(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        adapter = make_adapter(clock, radar)

        with pytest.raises(SensorAdapterError, match="polled before start"):
            adapter.get_detections()

    def test_polling_after_stop_returns_nothing(self, clock: SimClock, radar: StubRadar) -> None:
        adapter = make_adapter(clock, radar)
        adapter.start()
        adapter.stop()

        assert adapter.get_detections() == []

    def test_a_driver_failure_surfaces_as_an_adapter_error(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        """Translated rather than propagated: the pipeline is written against the adapter
        contract, and a driver-layer exception crossing it would make the boundary porous in the
        one direction nobody thinks to check."""
        adapter = make_adapter(clock, radar)
        adapter.start()
        radar.raise_on_poll = RadarSensorError("link is gone")

        with pytest.raises(SensorAdapterError, match="link is gone"):
            adapter.get_detections()


class TestCanonicalOutput:
    def test_the_sensor_type_is_a_generic_kind_not_a_model(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        """No manufacturer or model name reaches the canonical schema. The unit's designation
        travels in the raw log and in configuration, never in a Detection."""
        adapter = make_adapter(clock, radar)
        adapter.start()
        radar.queued = [target()]

        assert adapter.get_detections()[0].sensor.sensor_type is SensorType.RADAR

    def test_a_return_becomes_a_position_in_wgs84(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        adapter = make_adapter(clock, radar, mount=MountConfig(boresight_deg=0.0))
        adapter.start()
        radar.queued = [target(range_m=100.0, azimuth_deg=0.0)]

        position = adapter.get_detections()[0].position

        assert position.latitude > ORIGIN.latitude_deg, "due north of the sensor"
        assert position.longitude == pytest.approx(ORIGIN.longitude_deg, abs=1e-6)

    def test_mount_tilt_places_the_return_above_the_sensor(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        """The one thing that makes a tilted mount interpretable downstream. Without applying it
        the return would be placed on the ground at the full slant range — wrong in both
        altitude and horizontal position."""
        level = make_adapter(clock, radar, mount=MountConfig(tilt_deg=0.0))
        level.start()
        radar.queued = [target(range_m=100.0)]
        level_altitude = level.get_detections()[0].position.altitude_m

        tilted_radar = StubRadar()
        tilted = make_adapter(clock, tilted_radar, mount=MountConfig(tilt_deg=30.0))
        tilted.start()
        tilted_radar.queued = [target(range_m=100.0)]
        tilted_altitude = tilted.get_detections()[0].position.altitude_m

        assert tilted_altitude - level_altitude == pytest.approx(50.0, abs=0.5)

    def test_mount_height_raises_the_reported_altitude(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        adapter = make_adapter(clock, radar, mount=MountConfig(height_m=10.0))
        adapter.start()
        radar.queued = [target(range_m=50.0)]

        altitude = adapter.get_detections()[0].position.altitude_m

        assert altitude == pytest.approx(ORIGIN.altitude_m + 10.0, abs=0.5)

    def test_the_covariance_is_reported_and_dominated_by_the_vertical_term(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        """The adapter's central obligation for this sensor. A scalar uncertainty alone would
        collapse a beam-sized vertical unknown into the same number as a metre of range error."""
        adapter = make_adapter(clock, radar, mount=MountConfig(elevation_extent_deg=30.0))
        adapter.start()
        radar.queued = [target(range_m=100.0)]

        measurement = adapter.get_detections()[0].measurement

        assert measurement.position_covariance_enu is not None
        variances = [measurement.position_covariance_enu[i][i] for i in range(3)]
        assert variances[2] == max(variances)

    def test_the_scalar_uncertainty_does_not_understate_the_covariance(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        """The fallback a filter uses when it ignores the matrix. Understating it makes the
        filter overconfident, which is worse than reporting nothing at all."""
        adapter = make_adapter(clock, radar)
        adapter.start()
        radar.queued = [target(range_m=100.0)]

        measurement = adapter.get_detections()[0].measurement
        assert measurement.position_covariance_enu is not None
        largest = max(measurement.position_covariance_enu[i][i] for i in range(3))

        assert measurement.position_uncertainty_m == pytest.approx(math.sqrt(largest))

    def test_radial_velocity_is_carried_and_marked_measured(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        """Range rate is a quantity a Doppler-capable unit genuinely measures, unlike speed and
        heading, which is why the canonical schema carries it separately (ADR 0002)."""
        adapter = make_adapter(clock, radar)
        adapter.start()
        radar.queued = [target(velocity_mps=-12.5)]

        kinematics = adapter.get_detections()[0].kinematics

        assert kinematics is not None
        assert kinematics.radial_velocity_mps == pytest.approx(-12.5)
        assert kinematics.provenance is Provenance.MEASURED
        assert kinematics.speed_mps is None, "speed is not measured and must not be invented"
        assert kinematics.heading_deg is None, "heading is not measured and must not be invented"

    def test_no_classification_hint_is_emitted(self, clock: SimClock, radar: StubRadar) -> None:
        """The unit distinguishes nothing. A label from here would be an invention weighted as
        evidence downstream, and the classification module is built to work without one."""
        adapter = make_adapter(clock, radar)
        adapter.start()
        radar.queued = [target()]

        assert adapter.get_detections()[0].classification is None

    def test_confidence_is_the_configured_value_and_nothing_cleverer(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        """Flat and honest about being flat. No measured detection statistics exist for a small
        airframe against this unit, so there is nothing to compute a per-detection confidence
        from."""
        adapter = make_adapter(clock, radar, uncertainty=UncertaintyConfig(reported_confidence=0.4))
        adapter.start()
        radar.queued = [target(range_m=10.0), target(range_m=90.0)]

        assert [d.quality.confidence for d in adapter.get_detections()] == [0.4, 0.4]

    def test_detection_ids_are_unique_and_scoped_to_the_sensor(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        adapter = make_adapter(clock, radar)
        adapter.start()
        radar.queued = [target(range_m=10.0), target(range_m=20.0)]

        ids = [d.detection_id for d in adapter.get_detections()]

        assert len(set(ids)) == 2
        # The sensor ID is sanitised into the identifier, so separators do not survive.
        assert all("benchradar" in identifier for identifier in ids)


class TestTiming:
    def test_timestamps_come_from_the_injected_clock(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        adapter = make_adapter(clock, radar)
        adapter.start()
        clock.advance(30.0)
        radar.queued = [target(timestamp=clock.monotonic())]

        assert adapter.get_detections()[0].timestamp == clock.now()

    def test_a_returns_own_decode_time_is_preserved_rather_than_the_drain_time(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        """A poll can collect several frames spanning a cycle. Stamping them all with the drain
        time would compress them onto one instant, and a tracker deriving velocity from positions
        would read that as an object that teleported and then stopped."""
        adapter = make_adapter(clock, radar)
        adapter.start()
        early, late = clock.monotonic(), clock.monotonic() + 0.5
        clock.advance(1.0)
        radar.queued = [target(timestamp=early), target(timestamp=late)]

        first, second = adapter.get_detections()

        assert (second.timestamp - first.timestamp).total_seconds() == pytest.approx(0.5)
        assert first.timestamp < clock.now()

    def test_timestamps_are_timezone_aware_utc(self, clock: SimClock, radar: StubRadar) -> None:
        adapter = make_adapter(clock, radar)
        adapter.start()
        radar.queued = [target()]

        assert adapter.get_detections()[0].timestamp.tzinfo is not None


class TestHealth:
    def test_health_before_start_is_not_started(self, clock: SimClock, radar: StubRadar) -> None:
        """Distinct from OFFLINE: nothing has gone wrong yet."""
        assert make_adapter(clock, radar).health().state is SensorState.NOT_STARTED

    def test_a_connected_link_is_healthy(self, clock: SimClock, radar: StubRadar) -> None:
        adapter = make_adapter(clock, radar)
        adapter.start()

        assert adapter.health().state is SensorState.OK

    def test_a_reconnecting_link_is_degraded_not_offline(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        """It is expected back without intervention. An operator shown OFFLINE would go looking
        for a fault that is already resolving itself."""
        adapter = make_adapter(clock, radar)
        adapter.start()
        radar.state = RadarConnectionState.RECONNECTING

        assert adapter.health().state is SensorState.DEGRADED

    def test_a_faulted_link_is_offline(self, clock: SimClock, radar: StubRadar) -> None:
        adapter = make_adapter(clock, radar)
        adapter.start()
        radar.state = RadarConnectionState.FAULTED

        assert adapter.health().state is SensorState.OFFLINE

    def test_an_open_link_that_never_frames_is_degraded_and_names_the_baud_rate(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        """This integration's most likely first failure, and the one that is invisible in every
        obvious place: the link is up, nothing raises, and no detections appear. It has to be
        named here or it is indistinguishable from a quiet sky."""
        adapter = make_adapter(clock, radar)
        adapter.start()
        radar.status_counters = {
            "frames_decoded": 0,
            "bytes_discarded": 9000,
            "malformed_frames": 0,
        }

        health = adapter.health()

        assert health.state is SensorState.DEGRADED
        assert "baud rate" in health.reason

    def test_health_always_carries_a_reason(self, clock: SimClock, radar: StubRadar) -> None:
        adapter = make_adapter(clock, radar)
        adapter.start()

        assert adapter.health().reason

    def test_detection_counts_track_what_was_emitted(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        adapter = make_adapter(clock, radar)
        adapter.start()
        radar.queued = [target(range_m=10.0), target(range_m=20.0)]
        adapter.get_detections()

        health = adapter.health()

        assert health.detections_emitted == 2
        assert health.last_detection_time is not None

    def test_a_quiet_radar_emits_nothing_and_reports_no_detection_time(
        self, clock: SimClock, radar: StubRadar
    ) -> None:
        """Silence from a healthy link is data. Whether it means a fault or an empty sky is a
        site-level judgement made in `backend/system_health`, not here (FM-010)."""
        adapter = make_adapter(clock, radar)
        adapter.start()

        assert adapter.get_detections() == []
        assert adapter.health().last_detection_time is None
        assert adapter.health().state is SensorState.OK
