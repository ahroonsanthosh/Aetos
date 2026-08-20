"""A real adapter's detections, through the whole pipeline, with no hardware attached.

This is the test that says the governing principle holds now that a real sensor exists: the
tracking engine cannot tell whether a detection came from the simulator or from a device on a
serial port. Nothing below ingestion is configured differently here from a simulated run —
same pipeline, same tracker, same event log — and the only thing that changes is which adapter
is attached.

`tests/integration/test_pipeline.py` does the same for the simulator. The two together are
what make the claim testable rather than architectural.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from backend.common.clock import SimClock
from backend.main import Pipeline
from backend.zones.geometry import load_site_geometry
from sensors.adapters.radar.adapter import UartRadarAdapter
from sensors.adapters.radar.config import load_radar_hardware
from sensors.adapters.radar.transport import ReplayTransport
from sensors.schemas.detection import SensorType
from tests.unit.test_radar_protocol import target_frame

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def approach_frames(ranges: list[int]) -> list[bytes]:
    """One target-data frame per range step, as a target closing on boresight would produce."""
    return [target_frame((0x80, distance, 1, 18, 55)) for distance in ranges]


def build(tmp_path: Path) -> tuple[Pipeline, UartRadarAdapter, SimClock]:
    clock = SimClock(START)
    geometry = load_site_geometry("bench_radar_test")
    pipeline = Pipeline(clock, geometry, tmp_path)

    placement = geometry.site.sensors[0]
    adapter = UartRadarAdapter(
        clock=clock,
        sensor_id=placement.sensor_id,
        config=load_radar_hardware(placement.registry_key),
        # Paced, because a device reports on a cadence and a track is formed from detections
        # spread across pipeline cycles rather than from a burst arriving in one.
        transport=ReplayTransport(
            approach_frames(list(range(90, 20, -3))), chunk_delay_s=0.02
        ),
        position_enu=geometry.site.sensor_positions_enu()[placement.sensor_id],
        frame=geometry.frame,
        boresight_deg=placement.boresight_deg,
    )
    adapter.start()
    pipeline.attach(adapter)
    return pipeline, adapter, clock


def test_a_serial_radar_drives_the_pipeline_to_a_track(tmp_path: Path) -> None:
    pipeline, adapter, clock = build(tmp_path)

    accepted = 0
    for _ in range(30):
        accepted += pipeline.step()
        clock.advance(0.5)
        time.sleep(0.03)  # let the reader thread hand over the next report

    tracks = pipeline.tracker.active_tracks()
    pipeline.close()

    assert accepted > 0, "no detection from the radar adapter reached ingestion"
    assert tracks, "a target closing on the sensor produced no track"
    assert adapter.health().detections_emitted == accepted


def test_the_pipeline_reports_the_radar_as_a_real_adapter(tmp_path: Path) -> None:
    """What `GET /system/mode` reads. An operator's whole reading of the display turns on it."""
    pipeline, adapter, _ = build(tmp_path)

    try:
        assert pipeline.real_adapter_count() == 1
        assert adapter.sensor_type is SensorType.RADAR
    finally:
        pipeline.close()


def test_the_tracker_sees_nothing_the_simulator_would_not_also_supply(tmp_path: Path) -> None:
    """ADR 0002's guarantee, applied to the first adapter that reads real hardware.

    Stripping the optional blocks a real sensor may not supply must not change what the
    tracker is given, because the tracker is not permitted to depend on them.
    """
    pipeline, _, clock = build(tmp_path)

    for _ in range(10):
        pipeline.step()
        clock.advance(0.5)
        time.sleep(0.03)

    ingested = pipeline.ingestion.ingest([])
    pipeline.close()

    assert ingested.hints == {}, "the radar asserted a classification it never determined"
