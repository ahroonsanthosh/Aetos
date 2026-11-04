"""A camera as a sensor: motion found in the browser, turned into bearing-only detections."""

from sensors.adapters.camera.adapter import (
    FRAME_TIMEOUT_S,
    MAX_REGIONS_PER_FRAME,
    CameraAdapter,
    CameraFrame,
    MotionRegion,
)
from sensors.adapters.camera.config import CameraMount

__all__ = [
    "FRAME_TIMEOUT_S",
    "MAX_REGIONS_PER_FRAME",
    "CameraAdapter",
    "CameraFrame",
    "CameraMount",
    "MotionRegion",
]
