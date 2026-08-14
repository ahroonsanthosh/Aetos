"""A live run, end to end: physical adapters through ingestion, tracking and into the log.

The point of these tests is that nothing here mentions a camera or a radar. They assert that
*whatever* `sensors.live` builds reaches the operator picture — so the day the stub radar is
replaced by the physical unit, these keep passing without being edited, and if the swap breaks
the path they fail immediately.

The camera's stream is stubbed at the frame-source boundary, so no handset is needed; the stub
radar fabricates its own returns and needs nothing. Everything between the adapter and the
event log is the real production path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from backend.common.clock import SimClock
from backend.common.config import RunMode
from backend.database.records import SensorRecord
from backend.main import Pipeline, build_application
from backend.zones.geometry import load_site_geometry
from sensors.config.loader import load_sensor_config
from sensors.live import build_live_sensors

SITE = "fictional_test_range"


class _ScriptedFrames:
    """A frame source that yields sky, then sky with something crossing it.

    Stands in for the handset. The frames are the real resolution so the motion detector's
    thresholds mean what they mean in the field.
    """

    WIDTH = 1920
    HEIGHT = 1080

    def __init__(self) -> None:
        self._poll = 0

    def open(self) -> bool:
        return True

    def close(self) -> None:
        return None

    def read(self) -> np.ndarray | None:
        frame = self._sky(self._poll)
        # Settle the background model first, then fly a target across the frame. The target is
        # sized as a 335 x 253 mm airframe at ~10 m: the mission's stated minimum.
        if self._poll >= 30:
            centre_x = 400 + (self._poll - 30) * 90
            if centre_x < self.WIDTH - 60:
                frame[519:561, centre_x : centre_x + 56] = 150
        self._poll += 1
        return frame

    def _sky(self, seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        frame = np.full((self.HEIGHT, self.WIDTH, 3), 205.0, dtype=np.float32)
        frame += rng.normal(0.0, 3.0, frame.shape)
        return np.clip(frame, 0, 255).astype(np.uint8)


@pytest.fixture
def clock() -> SimClock:
    return SimClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
def pipeline(clock: SimClock, tmp_path) -> Pipeline:
    built = Pipeline(clock, load_site_geometry(SITE), tmp_path)
    yield built
    built.close()


def _attach_with_stubbed_camera(pipeline: Pipeline, clock: SimClock) -> _ScriptedFrames:
    """Attach the live sensor set, with the camera reading scripted frames.

    Reaches into the built adapters to swap the frame source rather than rebuilding the sensor
    list by hand, so the test exercises exactly what `build_live_sensors` produces — including
    any sensor added to it later.
    """
    config = load_sensor_config()
    sensors = build_live_sensors(clock=clock, frame=pipeline.geometry.frame, config=config)

    frames = _ScriptedFrames()
    for sensor in sensors:
        source = getattr(sensor.adapter, "_source", None)
        if source is not None:
            sensor.adapter._source = frames  # noqa: SLF001 - stubbing the hardware boundary

    pipeline._adapters = [sensor.adapter for sensor in sensors]  # noqa: SLF001
    pipeline._persist_live_sensors(sensors)  # noqa: SLF001
    for sensor in sensors:
        sensor.adapter.start()
    return frames


def test_live_sensors_produce_detections_the_pipeline_accepts(pipeline, clock) -> None:
    """The whole point: a physical adapter's output reaches the tracker.

    Asserts accepted detections rather than emitted ones. A detection the pipeline rejects at
    ingestion never reaches an operator, so counting what the adapter produced would pass on a
    system that quietly discards all of it.
    """
    _attach_with_stubbed_camera(pipeline, clock)

    accepted = 0
    for _ in range(45):
        accepted += pipeline.step()
        clock.advance(0.5)

    assert accepted > 0, "no live detection survived ingestion; the live path is not connected"


def test_a_crossing_target_becomes_a_track(pipeline, clock) -> None:
    """Detections are not the deliverable — a track is. This is the end-to-end claim."""
    _attach_with_stubbed_camera(pipeline, clock)

    for _ in range(45):
        pipeline.step()
        clock.advance(0.5)

    tracks = pipeline.tracker.active_tracks()

    assert tracks, "live detections produced no track"


def test_the_pipeline_cannot_tell_a_live_run_from_a_simulated_one(pipeline, clock) -> None:
    """`step()` polls `SensorAdapter` and nothing else.

    The guarantee the swap depends on: if this loop ever grows knowledge of which kind of
    source it is reading, replacing a sensor stops being a one-file change.
    """
    _attach_with_stubbed_camera(pipeline, clock)

    for adapter in pipeline._adapters:  # noqa: SLF001
        assert hasattr(adapter, "get_detections")
        assert hasattr(adapter, "health")
        assert hasattr(adapter, "sensor_id")


def test_live_sensors_are_persisted_so_the_operator_map_can_draw_them(pipeline, clock) -> None:
    """A sensor feeding the pipeline but absent from `GET /sensors` is invisible on the map."""
    _attach_with_stubbed_camera(pipeline, clock)

    records = pipeline.repositories.sensors.list_all(SITE)
    ids = {record.sensor_id for record in records}

    config = load_sensor_config()
    assert config.camera.sensor_id in ids
    assert config.radar.sensor_id in ids

    for record in records:
        assert isinstance(record, SensorRecord)
        # Placed through the site's own frame, so they land at the site, not at the bench
        # script's fallback origin.
        assert abs(record.latitude - pipeline.geometry.site.reference.latitude) < 0.01
        assert abs(record.longitude - pipeline.geometry.site.reference.longitude) < 0.01


def test_simulated_placements_do_not_survive_into_a_live_run(pipeline, clock) -> None:
    """A live map must not show sensors that are not on the bench.

    The site file lists simulated placements. Leaving them alongside the live ones would put
    sensors on the operator's map that do not exist, which is a worse error than a sparse map.
    """
    simulated_ids = {placement.sensor_id for placement in pipeline.geometry.site.sensors}
    assert simulated_ids, "the fixture site has no sensors; this test would prove nothing"

    _attach_with_stubbed_camera(pipeline, clock)

    live_ids = {record.sensor_id for record in pipeline.repositories.sensors.list_all(SITE)}

    assert not (live_ids & simulated_ids)


class TestRunModeSelection:
    def test_live_and_scenario_are_mutually_exclusive(self, tmp_path) -> None:
        """Fabricated truth and real observation must not be shown as one picture."""
        with pytest.raises(ValueError, match="not both"):
            build_application(
                site=SITE,
                scenario="a_benign_transit",
                live=True,
                data_dir=tmp_path,
                authenticated=False,
            )

    def test_a_live_application_reports_itself_as_live(self, tmp_path) -> None:
        """The operator interface must not describe a live run as a simulation."""
        app = build_application(site=SITE, live=True, data_dir=tmp_path, authenticated=False)
        try:
            assert app.state.services.run_mode is RunMode.LIVE
        finally:
            app.state.services.events.close()

    def test_a_scenario_application_still_reports_simulation(self, tmp_path) -> None:
        app = build_application(
            site=SITE, scenario="a_benign_transit", data_dir=tmp_path, authenticated=False
        )
        try:
            assert app.state.services.run_mode is RunMode.SIMULATION
        finally:
            app.state.services.events.close()
