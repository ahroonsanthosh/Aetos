"""Where something moved in the frame. No adapter state, no I/O, no model.

Plain functions over arrays, so this is testable with two still images and nothing else — no
stream, no handset, no network. That is the whole reason it is not a method on the adapter:
the CV logic is the part most worth iterating on, and it should be possible to iterate on it
against a directory of saved frames.

Two detectors, because they fail differently:

- `frame_difference` compares consecutive frames. Stateless, instant, and blind to anything
  that stops moving. Right for a two-image unit test and for a first look at a new scene.
- `BackgroundSubtractor` learns what the scene normally looks like (MOG2) and flags departures
  from it. Needs frames to warm up and adapts to gradual light change, which is what a camera
  outdoors actually faces. Right for the live adapter.

Neither knows what it found. A moving region is a region that moved — deciding whether it is a
drone, a bird or a branch is the classifier's job, and keeping that separation is what lets
either half be replaced without touching the other.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True, slots=True)
class MotionRegion:
    """An axis-aligned box, in pixels, around a contiguous region that changed.

    `x, y` is the top-left corner in image coordinates: x rightwards, y downwards.
    """

    x: int
    y: int
    width: int
    height: int

    @property
    def area_px(self) -> int:
        return self.width * self.height

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    def crop(self, frame: np.ndarray) -> np.ndarray:
        """The region's pixels, clamped to the frame so a box on the edge cannot overrun."""
        height, width = frame.shape[:2]
        x0 = max(0, min(self.x, width))
        y0 = max(0, min(self.y, height))
        x1 = max(x0, min(self.x + self.width, width))
        y1 = max(y0, min(self.y + self.height, height))
        return frame[y0:y1, x0:x1]


def to_grayscale(frame: np.ndarray) -> np.ndarray:
    """Single-channel view of a frame, whatever it arrived as.

    Colour carries nothing useful for motion and triples the work, so everything below this
    line is single-channel.
    """
    if frame.ndim == 2:
        return frame
    if frame.ndim == 3 and frame.shape[2] == 3:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if frame.ndim == 3 and frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
    raise ValueError(f"unsupported frame shape {frame.shape!r}")


def regions_from_mask(
    mask: np.ndarray,
    *,
    min_area_px: int,
) -> list[MotionRegion]:
    """Bounding boxes of the connected components of a binary mask, largest first.

    The area test is applied to the *contour* area rather than the bounding box, because a
    long thin diagonal streak — a compression artefact, a swaying wire — has a large box and a
    small footprint, and filtering on the box would keep exactly the things worth dropping.
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    regions: list[tuple[float, MotionRegion]] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area_px:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        regions.append((area, MotionRegion(int(x), int(y), int(w), int(h))))

    regions.sort(key=lambda item: item[0], reverse=True)
    return [region for _, region in regions]


def _clean(mask: np.ndarray) -> np.ndarray:
    """Drop speckle and close small gaps so one object yields one contour, not forty.

    Opening first removes isolated noise pixels; closing then joins the fragments of a single
    object that thresholding split apart. In the other order the closing would first weld the
    speckle into blobs too large for the opening to remove.
    """
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel, iterations=2)


def frame_difference(
    previous: np.ndarray,
    current: np.ndarray,
    *,
    sensitivity_threshold: int = 25,
    min_area_px: int = 400,
    blur_kernel: int = 5,
) -> list[MotionRegion]:
    """Regions that differ between two frames, largest first. Empty when nothing moved.

    Blurring before differencing is not cosmetic: sensor noise and JPEG artefacts produce
    per-pixel deltas of a few counts everywhere, and without the blur the threshold has to be
    raised so far to suppress them that real motion goes with it.
    """
    if previous.shape[:2] != current.shape[:2]:
        raise ValueError(
            f"frames differ in size: {previous.shape[:2]} vs {current.shape[:2]}; "
            "resize before comparing or the whole frame reads as motion"
        )

    a = cv2.GaussianBlur(to_grayscale(previous), (blur_kernel, blur_kernel), 0)
    b = cv2.GaussianBlur(to_grayscale(current), (blur_kernel, blur_kernel), 0)

    delta = cv2.absdiff(a, b)
    _, mask = cv2.threshold(delta, sensitivity_threshold, 255, cv2.THRESH_BINARY)
    return regions_from_mask(_clean(mask), min_area_px=min_area_px)


class BackgroundSubtractor:
    """MOG2 background model, wrapped so the adapter never touches the library directly.

    Stateful by nature — that is what distinguishes it from `frame_difference` — but the state
    is the background model and nothing else. It holds no reference to a stream, an adapter or
    a configuration object, so a test drives it by handing it frames in a loop.

    Shadow detection is on in the model and thresholded away in `apply()`. MOG2 marks shadows
    as a distinct grey value, and keeping that distinction and then discarding it is what stops
    a moving cloud shadow being reported as an object.
    """

    __slots__ = ("_model", "_min_area_px", "_frames_seen", "_warmup_frames")

    def __init__(
        self,
        *,
        history: int = 300,
        var_threshold: float = 32.0,
        min_area_px: int = 400,
        warmup_frames: int = 15,
    ) -> None:
        self._model = cv2.createBackgroundSubtractorMOG2(
            history=history,
            varThreshold=var_threshold,
            detectShadows=True,
        )
        self._min_area_px = min_area_px
        self._warmup_frames = warmup_frames
        self._frames_seen = 0

    @property
    def is_warm(self) -> bool:
        """Whether the model has seen enough frames for its output to mean anything.

        The first frames are almost entirely foreground by construction, since the model has
        no background yet. Reporting those would give a burst of spurious contacts every time
        the stream reconnects.
        """
        return self._frames_seen >= self._warmup_frames

    def apply(self, frame: np.ndarray, *, learning_rate: float = -1.0) -> list[MotionRegion]:
        """Update the model with a frame and return the regions that departed from it.

        `learning_rate=-1.0` lets the model choose its own rate from `history`. Pass 0.0 to
        freeze the background — useful when the scene is known to be about to change in a way
        that should not be learned.
        """
        mask = self._model.apply(to_grayscale(frame), learningRate=learning_rate)
        self._frames_seen += 1
        if not self.is_warm:
            return []

        # MOG2 writes 255 for foreground and 127 for shadow; keep only true foreground.
        _, binary = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
        return regions_from_mask(_clean(binary), min_area_px=self._min_area_px)
