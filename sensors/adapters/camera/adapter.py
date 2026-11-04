"""The camera adapter: moving regions in an image, out as canonical Detections.

The camera itself is the operator's own device. The console's Camera page opens it in the
browser, finds the regions of the image that moved, and sends those regions here; no video
leaves the browser. That is what makes a camera usable on a hosted deployment, where the
server has no camera of its own and cannot reach one on the operator's network.

## What a fixed monocular camera can honestly report

A **bearing** and an **elevation angle**, and nothing else. It measures no range, so it cannot
measure speed over ground either: apparent motion in the image is a function of range, and a
distant fast object and a near slow one are the same pixels. `kinematics` is therefore None
rather than filled with a figure derived from pixel motion.

The canonical schema requires a position, so the contact is placed at the configured
`assumed_range_m` along the measured line of sight, with an uncertainty that says so: the
along-line-of-sight term is the full configured range uncertainty, of the same order as the
range itself. Across the line of sight the camera is genuinely precise, a fraction of a degree,
so the error is strongly anisotropic and is reported as a full covariance. A scalar would be
applied equally in every direction and make the filter confident about a range nobody measured
(obligation 4 on `SensorAdapter`).

## Time

A frame is stamped when it arrives, from the injected clock, not with the browser's own clock,
which is not disciplined and may be minutes out. The cost is the upload latency, typically
well under a second, which the along-track uncertainty already dwarfs.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from backend.common.clock import Clock
from backend.common.geodesy import EnuPoint, GeodeticPoint, LocalFrame
from backend.common.identifiers import detection_id_generator
from sensors.adapters.camera.config import CameraMount
from sensors.adapters.interface import (
    SensorAdapter,
    SensorAdapterError,
    SensorHealth,
    SensorState,
)
from sensors.schemas.detection import (
    Detection,
    GeoPosition,
    Measurement,
    Quality,
    SensorRef,
    SensorType,
)

MIN_ANGULAR_SIGMA_DEG = 0.25
"""Floor on angular uncertainty however tight the region is. Lens distortion, mount flex and a
region's centre not being the object's centre all put a floor under it."""

FRAME_TIMEOUT_S = 5.0
"""No frame for this long and the camera reports DEGRADED: the page was closed, the tab put in
the background, or the network dropped. Silence must never read as an empty sky (FM-010)."""

MAX_QUEUED_FRAMES = 64
"""Frames held between polls. A browser that sends faster than the pipeline polls loses its
oldest frames first, so the picture stays current rather than falling behind."""

MAX_REGIONS_PER_FRAME = 16


@dataclass(frozen=True, slots=True)
class MotionRegion:
    """A box in the image where something moved, in pixels of the analysed frame."""

    x: float
    y: float
    width: float
    height: float
    strength: float
    """How much of the box changed, 0 to 1. Evidence the motion is real, not a confidence."""

    @property
    def centre(self) -> tuple[float, float]:
        return self.x + self.width / 2.0, self.y + self.height / 2.0


@dataclass(frozen=True, slots=True)
class CameraFrame:
    """One analysed frame: its size, and the regions that moved in it."""

    width: int
    height: int
    regions: tuple[MotionRegion, ...]


class CameraAdapter(SensorAdapter):
    """A camera whose frames are analysed elsewhere and handed in with `submit()`."""

    def __init__(self, clock: Clock, sensor_id: str, mount: CameraMount, session: str) -> None:
        super().__init__(clock, sensor_id, SensorType.EO_IR)
        self._mount = mount
        self._origin = LocalFrame(GeodeticPoint(mount.latitude, mount.longitude, mount.altitude_m))
        # Scoped by session so a camera restarted under the same id cannot reuse a detection
        # id the log already holds; ingestion would reject the duplicates and say so.
        self._ids = detection_id_generator(f"{sensor_id}-{session}")
        self._queue: deque[tuple[datetime, CameraFrame]] = deque(maxlen=MAX_QUEUED_FRAMES)

        self._running = False
        self._ever_started = False
        self._frames_received = 0
        self._last_frame_monotonic: float | None = None
        self._detections_emitted = 0
        self._last_detection_time: datetime | None = None

    @property
    def mount(self) -> CameraMount:
        return self._mount

    @property
    def frames_received(self) -> int:
        return self._frames_received

    def seconds_since_frame(self) -> float | None:
        if self._last_frame_monotonic is None:
            return None
        return self._clock.monotonic() - self._last_frame_monotonic

    # -- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ever_started = True
        self._last_frame_monotonic = self._clock.monotonic()

    def stop(self) -> None:
        self._running = False
        self._queue.clear()

    def submit(self, frame: CameraFrame) -> None:
        """Hand in one analysed frame. Stamped now, polled on the next cycle."""
        if not self._running:
            raise SensorAdapterError(f"camera {self.sensor_id} is not running")
        if len(frame.regions) > MAX_REGIONS_PER_FRAME:
            raise ValueError(f"at most {MAX_REGIONS_PER_FRAME} regions per frame")
        self._queue.append((self._clock.now(), frame))
        self._frames_received += 1
        self._last_frame_monotonic = self._clock.monotonic()

    def get_detections(self) -> list[Detection]:
        if not self._ever_started:
            raise SensorAdapterError(f"get_detections() before start() on {self.sensor_id!r}")
        if not self._running:
            return []

        detections: list[Detection] = []
        while self._queue:
            timestamp, frame = self._queue.popleft()
            detections.extend(self._to_detection(frame, r, timestamp) for r in frame.regions)

        if detections:
            self._detections_emitted += len(detections)
            self._last_detection_time = detections[-1].timestamp
        return detections

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

    def _assess(self) -> tuple[SensorState, str]:
        if not self._ever_started:
            return SensorState.NOT_STARTED, "camera not started"
        if not self._running:
            return SensorState.OFFLINE, "camera stopped"
        age = self.seconds_since_frame()
        if age is not None and age > FRAME_TIMEOUT_S:
            return (
                SensorState.DEGRADED,
                f"no frame from the browser for {age:.0f}s; the Camera page may be closed",
            )
        return SensorState.OK, "browser camera sending frames; bearing only, range assumed"

    # -- geometry -----------------------------------------------------------------------

    def _to_detection(
        self, frame: CameraFrame, region: MotionRegion, timestamp: datetime
    ) -> Detection:
        mount = self._mount
        azimuth_off, elevation_off = self._angles_off_axis(frame, region)
        bearing_rad = math.radians((mount.boresight_deg + math.degrees(azimuth_off)) % 360.0)
        elevation_rad = math.radians(mount.tilt_deg) + elevation_off

        r = mount.assumed_range_m
        ground = r * math.cos(elevation_rad)
        enu = EnuPoint(
            east_m=ground * math.sin(bearing_rad),
            north_m=ground * math.cos(bearing_rad),
            up_m=r * math.sin(elevation_rad),
        )
        geodetic = self._origin.to_geodetic(enu)

        angular_sigma = self._angular_sigma_rad(frame, region)
        along = mount.range_sigma_m * math.cos(elevation_rad)
        cross = r * angular_sigma
        vertical = math.hypot(mount.range_sigma_m * math.sin(elevation_rad), r * angular_sigma)
        covariance = _covariance_enu(bearing_rad, along, cross, vertical)

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
                # The worst axis, so a consumer ignoring the covariance is never more confident
                # than one that reads it.
                position_uncertainty_m=max(along, cross, vertical),
                position_covariance_enu=covariance,
            ),
            # Motion earns a floor; a box that changed throughout earns more. Never high: a
            # moving blob is a shadow, a bird or a branch often enough to matter.
            quality=Quality(confidence=0.3 + 0.35 * max(0.0, min(1.0, region.strength))),
            kinematics=None,
            classification=None,
        )

    def _angles_off_axis(self, frame: CameraFrame, region: MotionRegion) -> tuple[float, float]:
        """Horizontal and vertical angle of a region's centre off the optical axis, radians.

        A rectilinear pinhole: the tangent of the angle is proportional to the distance from
        the image centre. Interpolating linearly across the field of view is wrong by degrees
        at the frame edge, which is exactly where a contact entering the scene appears.
        """
        cx, cy = region.centre
        dx = (cx - frame.width / 2.0) / (frame.width / 2.0)
        # Image y grows downwards; elevation grows upwards.
        dy = (frame.height / 2.0 - cy) / (frame.height / 2.0)
        half_h = math.radians(self._mount.horizontal_fov_deg / 2.0)
        half_v = math.radians(self._mount.vertical_fov_deg / 2.0)
        return math.atan(dx * math.tan(half_h)), math.atan(dy * math.tan(half_v))

    def _angular_sigma_rad(self, frame: CameraFrame, region: MotionRegion) -> float:
        """How precisely the region fixes a direction: half its angular width, floored."""
        deg_per_px = self._mount.horizontal_fov_deg / max(frame.width, 1)
        return math.radians(max(region.width * deg_per_px / 2.0, MIN_ANGULAR_SIGMA_DEG))


def _covariance_enu(
    bearing_rad: float, along_m: float, cross_m: float, vertical_m: float
) -> list[list[float]]:
    """A 3x3 ENU covariance for an error ellipse aligned to the line of sight.

    Diagonal in the camera's own along/across frame, then rotated into ENU. Without the
    rotation the large range uncertainty would sit on the north axis whichever way the camera
    pointed.
    """
    sin_b, cos_b = math.sin(bearing_rad), math.cos(bearing_rad)
    var_a, var_c = along_m**2, cross_m**2
    ee = var_a * sin_b**2 + var_c * cos_b**2
    nn = var_a * cos_b**2 + var_c * sin_b**2
    en = (var_a - var_c) * sin_b * cos_b
    return [[ee, en, 0.0], [en, nn, 0.0], [0.0, 0.0, vertical_m**2]]
