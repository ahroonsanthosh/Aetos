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
    _clean,
    frame_difference,
    regions_from_mask,
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


class TestSmallAirborneTarget:
    """The mission target, at the sizes it actually presents.

    Every case here failed before the shadow, morphology and area-measure fixes, and each
    failed *silently* — an erased mask looks exactly like an empty sky. These are the
    regression tests for that, sized from the real airframe rather than from a convenient
    synthetic blob: 335 x 253 x 95 mm at 1920 px over 66 deg is ~56 x 42 px top-aspect and
    ~56 x 16 px side-aspect at 10 m, the stated minimum detection range.
    """

    # The stream's real resolution. The pixel sizes below are derived from it and from the
    # configured 66 deg field of view, so they only mean anything at this frame size.
    SKY_WIDTH = 1920
    SKY_HEIGHT = 1080

    TOP_ASPECT_10M = (56, 42)
    SIDE_ASPECT_10M = (56, 16)
    TOP_ASPECT_20M = (28, 21)

    @classmethod
    def _bright_sky(cls, *, noise_seed: int = 0) -> np.ndarray:
        """An overexposed sky, which is what a camera pointed upward actually sees."""
        rng = np.random.default_rng(noise_seed)
        frame = np.full((cls.SKY_HEIGHT, cls.SKY_WIDTH, 3), 205.0, dtype=np.float32)
        frame += rng.normal(0.0, 3.0, frame.shape)
        return np.clip(frame, 0, 255).astype(np.uint8)

    @classmethod
    def _airframe(
        cls, frame: np.ndarray, *, centre_x: int, size: tuple[int, int], tone: int
    ) -> np.ndarray:
        out = frame.copy()
        width, height = size
        x0 = centre_x - width // 2
        y0 = cls.SKY_HEIGHT // 2 - height // 2
        out[y0 : y0 + height, x0 : x0 + width] = tone
        return out

    def _traverse(self, *, size: tuple[int, int], tone: int, min_area_px: int = 150) -> int:
        """Polls in which the target was detected as it crosses the frame."""
        subtractor = BackgroundSubtractor(
            history=40, var_threshold=50.0, min_area_px=min_area_px, warmup_frames=15
        )
        for seed in range(25):
            subtractor.apply(self._bright_sky(noise_seed=seed))

        return sum(
            1
            for step, centre_x in enumerate(range(700, 1300, 100))
            if subtractor.apply(
                self._airframe(
                    self._bright_sky(noise_seed=200 + step),
                    centre_x=centre_x,
                    size=size,
                    tone=tone,
                )
            )
        )

    @pytest.mark.parametrize("tone", [40, 90, 150, 180])
    def test_detected_at_ten_metres_whatever_its_tone(self, tone: int) -> None:
        """The shadow regression, and the one that mattered most.

        MOG2's shadow test is "this pixel is the background scaled down in brightness", which
        describes any airframe paler than silhouette-black against bright sky. With shadow
        detection on and shadow pixels discarded, tones 110 and above were erased entirely —
        every pixel, before any threshold was consulted. A pale foam airframe is exactly that
        case, so the target the system exists to see was the one it could not see.
        """
        assert self._traverse(size=self.TOP_ASPECT_10M, tone=tone) == 6

    def test_detected_side_on_at_ten_metres(self) -> None:
        """Edge-on aspect collapses the height to ~16 px, which the 5x5 opening used to eat."""
        assert self._traverse(size=self.SIDE_ASPECT_10M, tone=150) == 6

    def test_still_detected_beyond_the_required_range(self) -> None:
        """20 m top-aspect is ~28 x 21 px. Margin beyond the requirement, not a target itself."""
        assert self._traverse(size=self.TOP_ASPECT_20M, tone=150) == 6

    def test_the_configured_threshold_would_have_missed_it(self) -> None:
        """Guards the sizing rule: the area gate must stay below what the target presents.

        3000 px was the value tuned against observed false-positive regions, and it put the
        target out of reach at every range past ~7 m. This asserts the trap rather than
        trusting a comment: whatever min_area_px becomes, it must not drift back above what a
        10 m contact actually produces.
        """
        assert self._traverse(size=self.TOP_ASPECT_10M, tone=150, min_area_px=3000) == 0


def test_a_thin_component_survives_cleaning() -> None:
    """Opening must not impose a minimum thickness on real targets.

    Erosion keeps only pixels where the kernel fits entirely inside the component, and the
    dilation that follows cannot restore what it removed. A 5x5 kernel therefore erased any
    component under 5 px in either axis — measured, a 100 x 4 blob survived with zero pixels.
    A 335 mm airframe seen edge-on is under 5 px tall beyond ~30 m.
    """
    mask = np.zeros((1080, 1920), dtype=np.uint8)
    mask[500:504, 800:900] = 255  # 100 x 4, the case that vanished

    regions = regions_from_mask(_clean(mask), min_area_px=150)

    assert len(regions) == 1
    assert regions[0].area_px >= 150


def test_region_area_is_the_true_pixel_count() -> None:
    """Area must not understate the footprint it is compared against.

    `cv2.contourArea` measures the area inside the approximated polygon, undercounting a small
    component by roughly its perimeter/2 — a 17 x 6 blob reports 74 against a true 94. Applied
    against min_area_px that rejected targets on area the measurement had itself shrunk.
    """
    mask = np.zeros((1080, 1920), dtype=np.uint8)
    mask[500:530, 800:850] = 255  # exactly 50 x 30 = 1500 px

    regions = regions_from_mask(mask, min_area_px=100)

    assert len(regions) == 1
    assert regions[0].area_px == 1500
