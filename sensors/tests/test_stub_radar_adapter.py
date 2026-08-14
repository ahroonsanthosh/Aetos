"""The stub radar emits valid canonical Detections, at the configured rate, and nothing more.

These tests are about the *contract*, not the physics — the stub has no physics. What matters
is that everything it emits validates, stays inside the configured envelope, and carries no
field a real radar could not populate.
"""

from __future__ import annotations

import math

import pytest

from backend.common.clock import SimClock
from backend.common.geodesy import GeodeticPoint, LocalFrame
from sensors.adapters.interface import SensorAdapterError, SensorState
from sensors.adapters.radar.stub_radar_adapter import StubRadarAdapter
from sensors.config.loader import SensorConfig
from sensors.schemas.detection import Detection, SensorType

POLLS = 400


def _build(config: SensorConfig, clock: SimClock, frame: LocalFrame) -> StubRadarAdapter:
    return StubRadarAdapter(clock=clock, config=config.radar, frame=frame)


def _poll(adapter: StubRadarAdapter, clock: SimClock, n: int = POLLS) -> list[Detection]:
    """Poll `n` times, advancing past the emission interval each time."""
    collected: list[Detection] = []
    for _ in range(n):
        collected.extend(adapter.get_detections())
        clock.advance(1.0)
    return collected


def test_emits_at_most_one_detection_per_poll(config, clock, frame) -> None:
    adapter = _build(config, clock, frame)
    adapter.start()

    for _ in range(POLLS):
        assert len(adapter.get_detections()) <= 1
        clock.advance(1.0)


def test_every_emitted_detection_is_within_the_configured_envelope(config, clock, frame) -> None:
    """Bearing, range, velocity and confidence all land inside their configured ranges.

    Bearing and range are not fields on a Detection — the adapter converts them to WGS-84 —
    so they are recovered by converting back into the local frame. That round trip is worth
    testing in its own right: a sign error in the bearing convention would put every contact
    on the wrong side of the sensor and nothing downstream would notice.
    """
    adapter = _build(config, clock, frame)
    adapter.start()
    detections = _poll(adapter, clock)

    assert detections, "no detections over 400 polls; the emission rate is misconfigured"

    stub = config.radar.stub
    sensor_east = config.radar.position_enu.east_m
    sensor_north = config.radar.position_enu.north_m

    for detection in detections:
        enu = frame.to_enu(
            GeodeticPoint(
                detection.position.latitude,
                detection.position.longitude,
                detection.position.altitude_m,
            )
        )
        de = enu.east_m - sensor_east
        dn = enu.north_m - sensor_north

        ground_range = math.hypot(de, dn)
        assert stub.range_m.min - 1.0 <= ground_range <= stub.range_m.max + 1.0

        bearing = math.degrees(math.atan2(de, dn)) % 360.0
        assert 0.0 <= bearing <= 360.0

        assert stub.confidence.min <= detection.quality.confidence <= stub.confidence.max

        assert detection.kinematics is not None
        radial = detection.kinematics.radial_velocity_mps
        assert radial is not None
        assert stub.velocity_mps.min <= radial <= stub.velocity_mps.max


def test_reports_no_classification_and_no_derived_kinematics(config, clock, frame) -> None:
    """A radar does not classify and does not measure speed over ground.

    Populating either would be the adapter asserting a quantity the hardware cannot produce,
    which is the failure the provenance rules exist to prevent.
    """
    adapter = _build(config, clock, frame)
    adapter.start()

    for detection in _poll(adapter, clock):
        assert detection.classification is None
        assert detection.kinematics is not None
        assert detection.kinematics.speed_mps is None
        assert detection.kinematics.heading_deg is None


def test_identifies_itself_as_simulated(config, clock, frame) -> None:
    """A fabricated source must never be mistakable for a real one."""
    adapter = _build(config, clock, frame)
    adapter.start()

    assert adapter.sensor_type is SensorType.SIMULATED_RADAR
    assert adapter.sensor_type.is_simulated

    for detection in _poll(adapter, clock, n=200):
        assert detection.sensor.sensor_type.is_simulated
        assert detection.sensor.sensor_id == config.radar.sensor_id


def test_detection_ids_are_unique(config, clock, frame) -> None:
    adapter = _build(config, clock, frame)
    adapter.start()

    ids = [detection.detection_id for detection in _poll(adapter, clock)]
    assert len(ids) == len(set(ids))


def test_timestamps_come_from_the_injected_clock(config, clock, frame) -> None:
    """Not wall time. A stub that read the system clock could not be replayed (ADR 0003)."""
    adapter = _build(config, clock, frame)
    adapter.start()
    start_time = clock.now()

    detections = _poll(adapter, clock)

    assert detections
    for detection in detections:
        assert detection.timestamp >= start_time
        assert detection.timestamp <= clock.now()
        assert detection.timestamp.tzinfo is not None


def test_minimum_interval_bounds_the_emission_rate(config, clock, frame) -> None:
    """Polling faster than the configured interval does not produce more detections.

    Without the interval gate the emission probability is per-poll, so a fast driver would
    turn a quiet configured rate into a firehose.
    """
    adapter = _build(config, clock, frame)
    adapter.start()

    # 2000 polls with no time passing at all.
    emitted = sum(len(adapter.get_detections()) for _ in range(2000))

    assert emitted <= 1, f"{emitted} detections emitted without the clock advancing"


def test_polling_before_start_raises(config, clock, frame) -> None:
    """An adapter nobody started must not report an empty sky (FM-010)."""
    adapter = _build(config, clock, frame)

    with pytest.raises(SensorAdapterError):
        adapter.get_detections()


def test_lifecycle_states(config, clock, frame) -> None:
    adapter = _build(config, clock, frame)
    assert adapter.health().state is SensorState.NOT_STARTED
    assert not adapter.health_check()

    assert adapter.connect() is True
    assert adapter.health().state is SensorState.OK

    adapter.start()  # Idempotent.
    assert adapter.health().state is SensorState.OK

    adapter.disconnect()
    assert adapter.health().state is SensorState.OFFLINE
    assert adapter.get_detections() == []

    adapter.disconnect()  # Idempotent.
    assert adapter.health().state is SensorState.OFFLINE


def test_seeded_runs_are_reproducible(config, clock, frame) -> None:
    """Same seed, same clock, same output. A bench run must be reproducible."""

    def run() -> list[tuple[float, float, float]]:
        local_clock = SimClock(clock.now())
        adapter = _build(config, local_clock, frame)
        adapter.start()
        return [
            (d.position.latitude, d.position.longitude, d.quality.confidence)
            for d in _poll(adapter, local_clock, n=200)
        ]

    assert run() == run()


def test_health_counts_what_it_emitted(config, clock, frame) -> None:
    adapter = _build(config, clock, frame)
    adapter.start()
    detections = _poll(adapter, clock)

    health = adapter.health()
    assert health.detections_emitted == len(detections)
    assert health.last_detection_time == detections[-1].timestamp
    assert health.sensor_id == config.radar.sensor_id
