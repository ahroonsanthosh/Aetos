"""The camera adapter: a video stream in, canonical Detections out.

This file owns I/O and geometry and nothing else. It opens a stream, pulls frames, hands them
to the motion detector, hands the moving regions to the classifier, and converts what comes
back into the canonical schema. The computer vision lives in `motion_detector.py` and the model
in `classifier.py`, so each of the three can be replaced without the other two noticing.

## What a fixed monocular camera can honestly report

A bearing, and that is all. It measures no range, so it cannot measure speed over ground
either, and `kinematics` is therefore left None rather than filled with a figure derived from
pixel motion — apparent motion in the image is a function of range, and a distant fast object
and a near slow one are the same pixels.

The canonical schema requires a position, so the adapter places the contact at the configured
nominal range along the measured bearing and attaches an uncertainty that says so: the
along-line-of-sight term is the full configured range uncertainty, which is the same order as
the range itself. That is not a hedge, it is the actual information content — a bearing line —
expressed in the one shape the pipeline reads. Understating it would make the downstream filter
confident about a range nobody measured (obligation 4 on `SensorAdapter`).

Cross-range is a different matter and genuinely precise: an angular measurement from a lens of
known field of view is good to a fraction of a degree. So the uncertainty is strongly
anisotropic, and it is reported as a full covariance rather than a scalar the filter would
apply equally in every direction.

## Stream handling

Streams over a home network drop, stall and return stale frames. The adapter distinguishes the
three: a read that fails puts it OFFLINE, a stream that keeps returning the same frame past
`frame_timeout_s` puts it DEGRADED, and neither is allowed to look like an empty sky.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Protocol

import cv2
import numpy as np

from backend.common.clock import Clock
from backend.common.geodesy import EnuPoint, LocalFrame
from backend.common.identifiers import detection_id_generator
from sensors.adapters.camera.classifier import Classifier
from sensors.adapters.camera.motion_detector import BackgroundSubtractor, MotionRegion
from sensors.adapters.interface import (
    SensorAdapter,
    SensorAdapterError,
    SensorHealth,
    SensorState,
)
from sensors.config.loader import CameraConfig
from sensors.schemas.detection import (
    ClassificationHint,
    Detection,
    GeoPosition,
    Measurement,
    Provenance,
    Quality,
    SensorRef,
    SensorType,
)

MIN_ANGULAR_SIGMA_DEG = 0.25
"""Floor on bearing uncertainty regardless of how tight the motion region is. A two-pixel box
implies an implausibly precise bearing; lens distortion, mount flex and the fact that the
region's centroid is not the object's centre all put a floor under it."""

VERTICAL_SIGMA_M = 200.0
"""Uncertainty on the up-axis. The camera measures no elevation usable as height without a
range, so the reported altitude is the mounting height and this says how little that means."""


class FrameSource(Protocol):
    """Whatever produces frames. Implemented by a real capture and by test doubles.

    Exists so the adapter can be tested without a handset on the network: the whole of the
    adapter's logic — motion, classification, geometry, health — runs against frames from an
    array in a list exactly as it runs against frames from a stream.
    """

    def open(self) -> bool:
        """Begin producing frames. False when the source is unreachable."""

    def read(self) -> np.ndarray | None:
        """The next frame, or None when none is available."""

    def close(self) -> None:
        """Release the source. Idempotent."""


class VideoCaptureFrameSource:
    """A frame source backed by a network video stream.

    The only place in the codebase that knows how the stream is transported. The URL is a
    configuration value and this class does not care what serves it.
    """

    __slots__ = ("_url", "_capture")

    def __init__(self, url: str) -> None:
        self._url = url
        self._capture: cv2.VideoCapture | None = None

    def open(self) -> bool:
        capture = cv2.VideoCapture(self._url)
        # Keep the decoder queue at one frame. A buffered stream hands back frames that are
        # seconds old, and a stale frame timestamped now is worse than no frame at all.
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            capture.release()
            return False
        self._capture = capture
        return True

    def read(self) -> np.ndarray | None:
        if self._capture is None:
            return None
        ok, frame = self._capture.read()
        return frame if ok else None

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None


class CameraAdapter(SensorAdapter):
    """One fixed camera at one place on one site.

    Construction is cheap and touches no network; `start()` opens the stream. Each
    `get_detections()` pulls the latest frame, runs motion detection over it, classifies the
    regions that moved, and emits one Detection per region.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        config: CameraConfig,
        frame: LocalFrame,
        frame_source: FrameSource | None = None,
        classifier: Classifier | None = None,
        motion_detector: BackgroundSubtractor | None = None,
    ) -> None:
        super().__init__(clock, config.sensor_id, SensorType.EO_IR)
        self._config = config
        self._frame = frame
        self._position = EnuPoint(
            config.position_enu.east_m,
            config.position_enu.north_m,
            config.position_enu.up_m,
        )
        self._source = frame_source or VideoCaptureFrameSource(config.stream_url)
        self._classifier = classifier or Classifier(
            model_name=config.classifier.model_name,
            confidence_threshold=config.classifier.confidence_threshold,
            inference_size_px=config.classifier.inference_size_px,
        )
        self._motion = motion_detector or BackgroundSubtractor(
            history=config.motion.background_history,
            var_threshold=config.motion.background_var_threshold,
            min_area_px=config.motion.min_area_px,
        )
        self._ids = detection_id_generator(config.sensor_id)

        self._running = False
        self._ever_started = False
        self._stream_open = False
        self._last_frame_monotonic: float | None = None
        self._detections_emitted = 0
        self._last_detection_time = None

    def start(self) -> None:
        if self._running:
            return
        self._ever_started = True
        self._running = True
        self._detections_emitted = 0
        self._last_detection_time = None
        self._last_frame_monotonic = None
        self._stream_open = self._source.open()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stream_open = False
        self._source.close()

    def health(self) -> SensorHealth:
        state, reason = self._assess()
        return SensorHealth(
            sensor_id=self.sensor_id,
            sensor_type=self.sensor_type,
            state=state,
            last_detection_time=self._last_detection_time,
            detections_emitted=self._detections_emitted,
            reason=reason,
        )

    def get_detections(self) -> list[Detection]:
        if not self._ever_started:
            raise SensorAdapterError(
                f"sensor {self.sensor_id} polled before start(): an adapter nobody started "
                "reports an empty sky, which is indistinguishable from a quiet one"
            )
        if not self._running or not self._stream_open:
            return []

        frame = self._source.read()
        if frame is None:
            # A read failure is a stream problem, reported through health(). Returning empty
            # is correct — there is nothing to report — but health() must not say OK.
            return []
        self._last_frame_monotonic = self._clock.monotonic()

        regions = self._motion.apply(frame)
        if not regions:
            return []

        timestamp = self._clock.now()
        detections = [self._to_detection(frame, region, timestamp) for region in regions]

        self._detections_emitted += len(detections)
        self._last_detection_time = timestamp
        return detections

    def _assess(self) -> tuple[SensorState, str]:
        if not self._ever_started:
            return SensorState.NOT_STARTED, "camera constructed but not started"
        if not self._running:
            return SensorState.OFFLINE, "camera stopped"
        if not self._stream_open:
            return SensorState.OFFLINE, "camera stream could not be opened"

        if self._last_frame_monotonic is not None:
            age = self._clock.monotonic() - self._last_frame_monotonic
            if age > self._config.frame_timeout_s:
                return (
                    SensorState.DEGRADED,
                    f"no frame decoded for {age:.1f}s; stream stalled or disconnected",
                )

        reason = self._classifier.unavailable_reason
        if reason is not None:
            # Motion detection still works, so the camera is observing — it just cannot say
            # what it is looking at. That is degraded capability, not an outage.
            return SensorState.DEGRADED, f"motion detection only, no classification: {reason}"

        if not self._motion.is_warm:
            return SensorState.DEGRADED, "background model still warming up; not yet reporting"

        return SensorState.OK, "camera streaming, motion detection and classification active"

    def _to_detection(
        self,
        frame: np.ndarray,
        region: MotionRegion,
        timestamp: datetime,
    ) -> Detection:
        """One moving region, as a canonical Detection."""
        classification = self._classifier.classify(region.crop(frame))

        bearing_deg = self._bearing_of(region, frame_width=frame.shape[1])
        bearing_rad = math.radians(bearing_deg)

        # Bearing is clockwise from north: north is the cosine component, east the sine.
        east = self._position.east_m + self._config.assumed_range_m * math.sin(bearing_rad)
        north = self._position.north_m + self._config.assumed_range_m * math.cos(bearing_rad)
        geodetic = self._frame.to_geodetic(EnuPoint(east, north, self._position.up_m))

        along_sigma = self._config.assumed_range_uncertainty_m
        cross_sigma = self._cross_range_sigma_m(region, frame_width=frame.shape[1])

        return Detection(
            detection_id=self._ids.next(),
            timestamp=timestamp,
            sensor=SensorRef(sensor_id=self.sensor_id, sensor_type=self.sensor_type),
            position=GeoPosition(
                latitude=geodetic.latitude_deg,
                longitude=geodetic.longitude_deg,
                altitude_m=geodetic.altitude_m,
            ),
            measurement=Measurement(
                # The scalar is the worse of the two axes. A consumer that ignores the
                # covariance must not end up more confident than one that reads it.
                position_uncertainty_m=max(along_sigma, cross_sigma, VERTICAL_SIGMA_M),
                position_covariance_enu=_covariance_enu(
                    bearing_rad=bearing_rad,
                    along_sigma_m=along_sigma,
                    cross_sigma_m=cross_sigma,
                    vertical_sigma_m=VERTICAL_SIGMA_M,
                ),
            ),
            quality=Quality(confidence=self._detection_confidence(classification.confidence)),
            # No range means no usable speed or heading. Apparent motion in the image is a
            # function of range, so deriving one here would be inventing a measurement.
            kinematics=None,
            classification=(
                ClassificationHint(
                    label=classification.label,
                    confidence=classification.confidence,
                    provenance=Provenance.MEASURED,
                )
                if classification.is_confident
                else None
            ),
        )

    def _bearing_of(self, region: MotionRegion, *, frame_width: int) -> float:
        """True bearing of a region's centre, degrees clockwise from north.

        A rectilinear pinhole approximation: the tangent of the angle off the optical axis is
        proportional to the distance from the image centre. Linear interpolation across the
        field of view is the usual shortcut and is wrong by degrees at the frame edge, which
        is precisely where a contact entering the scene appears.
        """
        centre_x, _ = region.centre
        half_fov_rad = math.radians(self._config.horizontal_fov_deg / 2.0)
        # Normalised horizontal offset from the optical axis, -1 at the left edge, +1 at right.
        offset = (centre_x - frame_width / 2.0) / (frame_width / 2.0)
        angle_rad = math.atan(offset * math.tan(half_fov_rad))
        return (self._config.boresight_deg + math.degrees(angle_rad)) % 360.0

    def _cross_range_sigma_m(self, region: MotionRegion, *, frame_width: int) -> float:
        """Positional uncertainty perpendicular to the line of sight, at the assumed range.

        Driven by how wide the region is in angle — a large blob localises its object less
        precisely than a small one — with a floor for effects a pixel count cannot see.
        """
        deg_per_px = self._config.horizontal_fov_deg / max(frame_width, 1)
        angular_sigma_deg = max(region.width * deg_per_px / 2.0, MIN_ANGULAR_SIGMA_DEG)
        return self._config.assumed_range_m * math.radians(angular_sigma_deg)

    def _detection_confidence(self, classification_confidence: float) -> float:
        """Confidence that this is a real object rather than clutter.

        Motion alone earns a floor: something in the scene genuinely changed. A recognised
        object raises it, because the model agreeing that the pixels look like a thing is
        independent evidence that the region is not a shadow or a compression artefact.

        Deliberately never 1.0. A camera that reports certainty about a moving blob is wrong
        often enough that the number would poison anything weighting by confidence.
        """
        return min(0.95, 0.4 + 0.5 * classification_confidence)


def _covariance_enu(
    *,
    bearing_rad: float,
    along_sigma_m: float,
    cross_sigma_m: float,
    vertical_sigma_m: float,
) -> list[list[float]]:
    """A 3x3 ENU covariance for an error ellipse aligned to the line of sight.

    Built diagonal in the sensor's own along-/across-line-of-sight frame, then rotated into
    ENU. Skipping the rotation would put the large range uncertainty on the north axis
    regardless of where the camera is pointing, which is right only for a camera facing north.
    """
    sin_b, cos_b = math.sin(bearing_rad), math.cos(bearing_rad)

    # Unit vectors in ENU: along the line of sight, and 90 degrees clockwise from it.
    along_e, along_n = sin_b, cos_b
    cross_e, cross_n = cos_b, -sin_b

    var_along = along_sigma_m**2
    var_cross = cross_sigma_m**2

    ee = var_along * along_e**2 + var_cross * cross_e**2
    nn = var_along * along_n**2 + var_cross * cross_n**2
    en = var_along * along_e * along_n + var_cross * cross_e * cross_n

    return [
        [ee, en, 0.0],
        [en, nn, 0.0],
        [0.0, 0.0, vertical_sigma_m**2],
    ]
