"""Motion detection, driven entirely by synthesised still images.

No stream, no handset, no network — which is the point of keeping the CV logic out of the
adapter. Frames are built with numpy so the tests carry no binary fixtures and the exact
content of every frame is visible in the test that uses it.
"""

from __future__ import annotations

import numpy as np
import pytest

from sensors.adapters.camera.motion_detector import (
    BackgroundSubtractor,
    MotionRegion,
    frame_difference,
    to_grayscale,
)

FRAME_HEIGHT = 480
FRAME_WIDTH = 640


def _scene(*, noise_seed: int = 0) -> np.ndarray:
    """A plausible static background: a gradient sky with light per-pixel sensor noise.

    Flat grey would be an unfairly easy background — real thresholds have to survive noise,
    and a detector tuned against a noiseless frame falls apart on the first real one.
    """
    rng = np.random.default_rng(noise_seed)
    gradient = np.linspace(90, 160, FRAME_HEIGHT, dtype=np.float32)
    frame = np.repeat(gradient[:, None], FRAME_WIDTH, axis=1)
    frame = frame[:, :, None].repeat(3, axis=2)
    frame += rng.normal(0.0, 2.0, frame.shape)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _with_object(
    frame: np.ndarray,
    *,
    centre: tuple[int, int] = (320, 240),
    size: int = 40,
    value: int = 20,
) -> np.ndarray:
    """The same frame with a dark square added — a small dark object against a bright sky."""
    out = frame.copy()
    x, y = centre
    half = size // 2
    out[y - half : y + half, x - half : x + half] = value
    return out


def test_identical_frames_report_no_motion() -> None:
    """The case that matters most. A detector that fires on a still scene is useless."""
    frame = _scene()

    assert frame_difference(frame, frame, min_area_px=400) == []


def test_noise_between_frames_is_not_motion() -> None:
    """Two frames of the same scene differ by sensor noise and must still read as still."""
    before = _scene(noise_seed=1)
    after = _scene(noise_seed=2)

    assert frame_difference(before, after, min_area_px=400) == []


def test_an_added_object_is_detected_where_it_was_added() -> None:
    before = _scene()
    after = _with_object(before, centre=(200, 150), size=60)

    regions = frame_difference(before, after, min_area_px=400)

    assert len(regions) == 1
    region = regions[0]
    centre_x, centre_y = region.centre
    assert centre_x == pytest.approx(200, abs=8)
    assert centre_y == pytest.approx(150, abs=8)
    assert region.area_px >= 400


def test_an_object_below_the_area_threshold_is_ignored() -> None:
    """A few changed pixels are noise, foliage or compression, not a contact."""
    before = _scene()
    after = _with_object(before, size=8)

    assert frame_difference(before, after, min_area_px=400) == []


def test_two_separated_objects_are_two_regions_largest_first() -> None:
    before = _scene()
    after = _with_object(before, centre=(120, 120), size=40)
    after = _with_object(after, centre=(500, 360), size=80)

    regions = frame_difference(after, before, min_area_px=400)

    assert len(regions) == 2
    assert regions[0].area_px > regions[1].area_px


def test_mismatched_frame_sizes_raise_rather_than_reporting_the_whole_frame() -> None:
    """Silently resizing would turn a stream resolution change into a full-frame contact."""
    with pytest.raises(ValueError, match="differ in size"):
        frame_difference(_scene(), np.zeros((240, 320, 3), dtype=np.uint8))


def test_sensitivity_threshold_changes_what_counts_as_changed() -> None:
    """A faint object is found at high sensitivity and missed at low."""
    before = _scene()
    # A shallow change: about 12 counts against a background near 120.
    after = _with_object(before, size=60, value=110)

    assert frame_difference(before, after, sensitivity_threshold=5, min_area_px=400)
    assert frame_difference(before, after, sensitivity_threshold=80, min_area_px=400) == []


def test_grayscale_accepts_colour_and_single_channel_frames() -> None:
    assert to_grayscale(_scene()).ndim == 2
    assert to_grayscale(np.zeros((10, 10), dtype=np.uint8)).shape == (10, 10)

    with pytest.raises(ValueError):
        to_grayscale(np.zeros((10, 10, 5), dtype=np.uint8))


def test_region_crop_is_clamped_to_the_frame() -> None:
    """A box overhanging the edge yields the pixels that exist, not an error or a wrap."""
    frame = _scene()
    region = MotionRegion(x=FRAME_WIDTH - 20, y=FRAME_HEIGHT - 20, width=100, height=100)

    crop = region.crop(frame)

    assert crop.shape[:2] == (20, 20)


class TestBackgroundSubtractor:
    def test_reports_nothing_while_warming_up(self) -> None:
        """The first frames are all foreground by construction; reporting them is spurious."""
        subtractor = BackgroundSubtractor(min_area_px=400, warmup_frames=10)
        frame = _scene()

        for _ in range(9):
            assert subtractor.apply(frame) == []
        assert not subtractor.is_warm

    def test_a_learned_still_scene_reports_no_motion(self) -> None:
        subtractor = BackgroundSubtractor(min_area_px=400, warmup_frames=10)

        for seed in range(40):
            subtractor.apply(_scene(noise_seed=seed))

        assert subtractor.is_warm
        assert subtractor.apply(_scene(noise_seed=99)) == []

    def test_an_object_appearing_in_a_learned_scene_is_detected(self) -> None:
        subtractor = BackgroundSubtractor(min_area_px=400, warmup_frames=10)

        for seed in range(40):
            subtractor.apply(_scene(noise_seed=seed))

        regions = subtractor.apply(_with_object(_scene(noise_seed=99), centre=(400, 200), size=60))

        assert len(regions) == 1
        centre_x, centre_y = regions[0].centre
        assert centre_x == pytest.approx(400, abs=10)
        assert centre_y == pytest.approx(200, abs=10)
