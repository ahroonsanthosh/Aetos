"""The camera adapter, driven by a scripted frame source. No live stream, no model, no network.

Both heavy collaborators are injected: the frames come from a list and the classifier is a
stand-in. That is what the split between `camera_adapter`, `motion_detector` and `classifier`
buys — the adapter's own logic (geometry, uncertainty, lifecycle, health) is testable without
either a handset or a downloaded model.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from backend.common.geodesy import GeodeticPoint
from sensors.adapters.camera.camera_adapter import CameraAdapter
from sensors.adapters.camera.classifier import UNKNOWN, Classification
from sensors.adapters.camera.motion_detector import BackgroundSubtractor
from sensors.adapters.interface import SensorAdapterError, SensorState
from sensors.schemas.detection import ClassificationLabel, SensorType

FRAME_HEIGHT = 480
FRAME_WIDTH = 640


def _scene(*, noise_seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(noise_seed)
    gradient = np.linspace(90, 160, FRAME_HEIGHT, dtype=np.float32)
    frame = np.repeat(gradient[:, None], FRAME_WIDTH, axis=1)[:, :, None].repeat(3, axis=2)
    frame += rng.normal(0.0, 2.0, frame.shape)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _with_object(frame: np.ndarray, *, centre_x: int, size: int = 60) -> np.ndarray:
    out = frame.copy()
    half = size // 2
    out[240 - half : 240 + half, centre_x - half : centre_x + half] = 20
    return out


class ScriptedFrameSource:
    """Hands back a fixed list of frames, then None. Stands in for the video stream."""

    def __init__(self, frames: list[np.ndarray], *, opens: bool = True) -> None:
        self._frames = list(frames)
        self._opens = opens
        self.closed = False
        self.open_calls = 0

    def open(self) -> bool:
        self.open_calls += 1
        return self._opens

    def read(self) -> np.ndarray | None:
        return self._frames.pop(0) if self._frames else None

    def close(self) -> None:
        self.closed = True


class StubClassifier:
    """A classifier that always says the same thing, without loading anything."""

    def __init__(self, result: Classification = UNKNOWN, reason: str | None = None) -> None:
        self.result = result
        self._reason = reason
        self.crops: list[np.ndarray] = []

    @property
    def is_available(self) -> bool:
        return self._reason is None

    @property
    def unavailable_reason(self) -> str | None:
        return self._reason

    def classify(self, crop: np.ndarray) -> Classification:
        self.crops.append(crop)
        return self.result


def _build(config, clock, frame, *, source, classifier=None, warmup=5) -> CameraAdapter:
    return CameraAdapter(
        clock=clock,
        config=config.camera,
        frame=frame,
        frame_source=source,
        classifier=classifier or StubClassifier(),
        motion_detector=BackgroundSubtractor(
            min_area_px=config.camera.motion.min_area_px,
            warmup_frames=warmup,
        ),
    )


def _warm(adapter: CameraAdapter, clock, polls: int = 30) -> None:
    """Poll through the background model's warm-up, discarding whatever it says."""
    for _ in range(polls):
        adapter.get_detections()
        clock.advance(0.1)


def test_a_still_scene_produces_no_detections(config, clock, frame) -> None:
    source = ScriptedFrameSource([_scene(noise_seed=i) for i in range(60)])
    adapter = _build(config, clock, frame, source=source)
    adapter.start()
    _warm(adapter, clock)

    for _ in range(20):
        assert adapter.get_detections() == []
        clock.advance(0.1)


def test_a_moving_object_produces_one_detection(config, clock, frame) -> None:
    frames = [_scene(noise_seed=i) for i in range(40)]
    frames.append(_with_object(_scene(noise_seed=99), centre_x=FRAME_WIDTH // 2))
    source = ScriptedFrameSource(frames)

    adapter = _build(config, clock, frame, source=source)
    adapter.start()
    _warm(adapter, clock, polls=40)

    detections = adapter.get_detections()

    assert len(detections) == 1
    detection = detections[0]
    assert detection.sensor.sensor_type is SensorType.EO_IR
    assert detection.sensor.sensor_id == config.camera.sensor_id
    assert 0.0 <= detection.quality.confidence <= 1.0
    assert detection.timestamp == clock.now()


def test_a_fixed_camera_reports_no_kinematics(config, clock, frame) -> None:
    """No range means no honest speed. Deriving one from pixel motion would invent a fact."""
    frames = [_scene(noise_seed=i) for i in range(40)]
    frames.append(_with_object(_scene(noise_seed=99), centre_x=FRAME_WIDTH // 2))

    adapter = _build(config, clock, frame, source=ScriptedFrameSource(frames))
    adapter.start()
    _warm(adapter, clock, polls=40)

    detection = adapter.get_detections()[0]

    assert detection.kinematics is None


def test_horizontal_position_in_the_frame_becomes_a_bearing(config, clock, frame) -> None:
    """An object left of the optical axis lands west of one on the axis, and vice versa.

    This is the geometry that would silently mirror the whole scene if the sign convention
    were wrong, so it is asserted against a recovered bearing rather than a coordinate.
    """
    bearings: list[float] = []

    for centre_x in (FRAME_WIDTH // 4, FRAME_WIDTH // 2, 3 * FRAME_WIDTH // 4):
        frames = [_scene(noise_seed=i) for i in range(40)]
        frames.append(_with_object(_scene(noise_seed=99), centre_x=centre_x))

        adapter = _build(config, clock, frame, source=ScriptedFrameSource(frames))
        adapter.start()
        _warm(adapter, clock, polls=40)

        detection = adapter.get_detections()[0]
        enu = frame.to_enu(
            GeodeticPoint(
                detection.position.latitude,
                detection.position.longitude,
                detection.position.altitude_m,
            )
        )
        de = enu.east_m - config.camera.position_enu.east_m
        dn = enu.north_m - config.camera.position_enu.north_m
        # Signed bearing relative to the configured boresight, in (-180, 180].
        offset = (math.degrees(math.atan2(de, dn)) - config.camera.boresight_deg + 180.0)
        bearings.append(offset % 360.0 - 180.0)

    left, centre, right = bearings
    assert left < centre < right
    assert centre == pytest.approx(0.0, abs=1.0)
    assert abs(left) <= config.camera.horizontal_fov_deg / 2.0
    assert abs(right) <= config.camera.horizontal_fov_deg / 2.0


def test_range_uncertainty_dominates_and_is_reported_as_a_covariance(config, clock, frame) -> None:
    """The camera measures a bearing, not a position, and the covariance must say so."""
    frames = [_scene(noise_seed=i) for i in range(40)]
    frames.append(_with_object(_scene(noise_seed=99), centre_x=FRAME_WIDTH // 2))

    adapter = _build(config, clock, frame, source=ScriptedFrameSource(frames))
    adapter.start()
    _warm(adapter, clock, polls=40)

    measurement = adapter.get_detections()[0].measurement
    covariance = measurement.position_covariance_enu

    assert covariance is not None
    # Boresight is north in the checked-in config, so the along-line-of-sight term — the
    # assumed range uncertainty — lands on the north axis and dwarfs the cross-range east one.
    assert covariance[1][1] == pytest.approx(config.camera.assumed_range_uncertainty_m**2, rel=0.01)
    assert covariance[1][1] > covariance[0][0]
    assert measurement.position_uncertainty_m >= math.sqrt(covariance[0][0])


def test_classification_is_attached_when_the_model_is_confident(config, clock, frame) -> None:
    frames = [_scene(noise_seed=i) for i in range(40)]
    frames.append(_with_object(_scene(noise_seed=99), centre_x=FRAME_WIDTH // 2))
    classifier = StubClassifier(Classification(ClassificationLabel.BIRD, 0.8))

    adapter = _build(
        config, clock, frame, source=ScriptedFrameSource(frames), classifier=classifier
    )
    adapter.start()
    _warm(adapter, clock, polls=40)

    detection = adapter.get_detections()[0]

    assert detection.classification is not None
    assert detection.classification.label is ClassificationLabel.BIRD
    assert detection.classification.confidence == pytest.approx(0.8)
    assert classifier.crops, "the classifier was never handed a crop"


def test_an_unavailable_classifier_still_produces_detections(config, clock, frame) -> None:
    """Motion happened. The camera says so and admits it cannot name what moved (FM-010)."""
    frames = [_scene(noise_seed=i) for i in range(40)]
    frames.append(_with_object(_scene(noise_seed=99), centre_x=FRAME_WIDTH // 2))
    classifier = StubClassifier(reason="ultralytics is not installed")

    adapter = _build(
        config, clock, frame, source=ScriptedFrameSource(frames), classifier=classifier
    )
    adapter.start()
    _warm(adapter, clock, polls=40)

    detections = adapter.get_detections()

    assert len(detections) == 1
    assert detections[0].classification is None
    health = adapter.health()
    assert health.state is SensorState.DEGRADED
    assert "ultralytics" in health.reason


def test_an_unreachable_stream_reports_offline_not_an_empty_sky(config, clock, frame) -> None:
    source = ScriptedFrameSource([], opens=False)
    adapter = _build(config, clock, frame, source=source)

    assert adapter.connect() is False
    assert adapter.get_detections() == []
    assert adapter.health().state is SensorState.OFFLINE


def test_a_stalled_stream_reports_degraded(config, clock, frame) -> None:
    """Frames stop arriving but the socket is still open. Silence must not read as OK."""
    source = ScriptedFrameSource([_scene()])
    adapter = _build(config, clock, frame, source=source)
    adapter.start()

    adapter.get_detections()
    clock.advance(config.camera.frame_timeout_s + 1.0)
    adapter.get_detections()  # The source is exhausted; read() returns None.

    assert adapter.health().state is SensorState.DEGRADED
    assert "stalled" in adapter.health().reason


def test_polling_before_start_raises(config, clock, frame) -> None:
    adapter = _build(config, clock, frame, source=ScriptedFrameSource([_scene()]))

    with pytest.raises(SensorAdapterError):
        adapter.get_detections()


def test_lifecycle_releases_the_stream(config, clock, frame) -> None:
    source = ScriptedFrameSource([_scene() for _ in range(5)])
    adapter = _build(config, clock, frame, source=source)

    assert adapter.health().state is SensorState.NOT_STARTED
    adapter.connect()
    adapter.start()  # Idempotent: must not reopen the stream.
    assert source.open_calls == 1

    adapter.disconnect()
    assert source.closed
    assert adapter.health().state is SensorState.OFFLINE
    assert adapter.get_detections() == []
