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
import threading
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

_READ_RETRY_BACKOFF_S = 0.05
"""Pause after a failed read, so a down stream is not retried in a hot loop."""

_FAILURES_BEFORE_RECONNECT = 20
"""Consecutive failed reads before the reader reopens the stream. At the retry backoff above
that is about a second of solid failure — long enough not to react to a single dropped packet,
short enough that a genuine disconnect costs one second of observation rather than the run."""

_RECONNECT_BACKOFF_S = 0.5
"""Pause before reopening, so a handset that is off the network is not hammered."""

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
    """A frame source backed by a network video stream, always reporting the newest frame.

    The only place in the codebase that knows how the stream is transported. The URL is a
    configuration value and this class does not care what serves it.

    ## Why there is a thread in here

    A video stream is a queue, and a sensor is not. The stream delivers frames at its own rate
    — 30/s is typical — while the pipeline polls the adapter on its own cadence, which is far
    slower. Reading one queued frame per poll consumes the stream at the *polling* rate, so the
    reader falls further behind real time with every poll and ends up watching the past in slow
    motion. On a bench run at a 1.5s poll against a 30fps stream that is ~44 frames of lag
    added per poll: within a minute the adapter is reporting motion that happened long ago, and
    motion happening now never surfaces at all.

    `CAP_PROP_BUFFERSIZE` looks like the fix and is not. It is honoured by the DShow and V4L2
    backends and ignored by FFMPEG, which is the backend serving `http://` streams — so on
    exactly the transport where the lag matters, setting it does nothing.

    So a reader thread consumes the stream continuously and keeps only the most recent frame.
    Frames nobody polled for are dropped, which is the correct behaviour for a sensor: a frame
    that arrived while the pipeline was busy is stale data, not a backlog to work through. What
    `read()` returns is always the freshest view of the world available.

    ## Consuming it

    `read()` returns None when no *new* frame has arrived since the last call, rather than
    handing back the previous one again. That keeps a stalled stream distinguishable from a
    still scene: `CameraAdapter` ages its last successful read and reports DEGRADED once
    `frame_timeout_s` passes, which only works if silence stays silent.
    """

    __slots__ = (
        "_url",
        "_capture",
        "_latest",
        "_lock",
        "_stop",
        "_thread",
        "_frames_received",
        "_frames_dropped",
        "_read_failures",
        "_consecutive_failures",
        "_reconnects",
    )

    def __init__(self, url: str) -> None:
        self._url = url
        self._capture: cv2.VideoCapture | None = None
        self._latest: np.ndarray | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frames_received = 0
        self._frames_dropped = 0
        self._read_failures = 0
        self._consecutive_failures = 0
        self._reconnects = 0

    @property
    def frames_received(self) -> int:
        """Frames pulled off the stream since `open()`, whether or not anybody polled them."""
        return self._frames_received

    @property
    def frames_dropped(self) -> int:
        """Frames superseded before the pipeline polled for them.

        Expected to be large and rising — it is the stream's frame rate minus the poll rate,
        not a fault. A count near zero means the stream is delivering no faster than the
        pipeline consumes, which for a live camera usually means the stream is struggling.
        """
        return self._frames_dropped

    @property
    def read_failures(self) -> int:
        """Failed reads since `open()`. A steadily rising count is a failing stream."""
        return self._read_failures

    @property
    def reconnects(self) -> int:
        """Times the reader has reopened the stream after sustained failure."""
        return self._reconnects

    def open(self) -> bool:
        capture = cv2.VideoCapture(self._url)
        if not capture.isOpened():
            capture.release()
            return False

        self._capture = capture
        self._frames_received = 0
        self._frames_dropped = 0
        self._read_failures = 0
        self._consecutive_failures = 0
        self._reconnects = 0
        self._stop.clear()
        # Daemon: a reader thread must never be the reason the process refuses to exit.
        self._thread = threading.Thread(
            target=self._pump, name=f"frame-reader-{self._url}", daemon=True
        )
        self._thread.start()
        return True

    def _pump(self) -> None:
        """Consume the stream as fast as it arrives, keeping only the newest frame.

        Survives a failing stream rather than exiting on it. A decode error over a phone
        hotspot is routine — a dropped packet, a moment of congestion — and a reader that
        returned on the first one would leave the adapter permanently silent with no way back,
        since `open()` is only called from `start()`. Silence that looks like an empty sky is
        the failure this whole class of code exists to avoid (FM-010).
        """
        while not self._stop.is_set():
            capture = self._capture
            if capture is None:
                return
            try:
                ok, frame = capture.read()
            except cv2.error:
                # Distinguish the shutdown race from a stream fault: during close() the
                # capture is released underneath a blocked read, and stopping is then correct.
                if self._stop.is_set() or self._capture is None:
                    return
                ok, frame = False, None

            if not ok:
                self._read_failures += 1
                if self._reconnect_due():
                    self._reconnect()
                    continue
                # Back off rather than spinning hot on a stream that is down or has ended.
                self._stop.wait(_READ_RETRY_BACKOFF_S)
                continue

            self._consecutive_failures = 0
            with self._lock:
                if self._latest is not None:
                    self._frames_dropped += 1
                self._latest = frame
                self._frames_received += 1

    def _reconnect_due(self) -> bool:
        """Whether enough consecutive failures have accrued to justify reopening the stream."""
        self._consecutive_failures += 1
        return self._consecutive_failures >= _FAILURES_BEFORE_RECONNECT

    def _reconnect(self) -> None:
        """Reopen the capture in place, from inside the reader thread.

        A stream that has genuinely dropped does not recover by being read at harder — the
        socket is gone and every subsequent read fails identically. Reopening is the only
        route back, and doing it here rather than in `start()` is what lets a run survive the
        handset changing networks or the app being restarted mid-flight.
        """
        self._consecutive_failures = 0
        self._reconnects += 1

        previous, self._capture = self._capture, None
        if previous is not None:
            previous.release()
        if self._stop.wait(_RECONNECT_BACKOFF_S):
            return

        capture = cv2.VideoCapture(self._url)
        if capture.isOpened():
            self._capture = capture
        else:
            capture.release()
            # Leave _capture None and let the loop exit; health reports the stream as down
            # rather than pretending a dead source is merely quiet.

    def read(self) -> np.ndarray | None:
        """The newest frame since the last call, or None if no new frame has arrived."""
        with self._lock:
            frame = self._latest
            self._latest = None
        return frame

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            # Bounded: a read blocked on a dead network must not hang shutdown. The thread is
            # a daemon, so the worst case is it is still unwinding as the process exits.
            thread.join(timeout=2.0)
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        with self._lock:
            self._latest = None


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
