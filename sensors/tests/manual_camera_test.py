"""Manual bench check: point the camera adapter at the real stream and watch it fire.

Not part of the automated suite — it needs a handset on the network and a human in front of
the lens, so it is excluded from collection in `conftest.py` and run by hand:

    python -m sensors.tests.manual_camera_test

What it answers is the one thing the unit tests cannot: whether the stream opens, whether the
background model settles, and whether waving something in front of the phone actually produces
a Detection. Everything it prints comes from the adapter through its normal interface — no
stream handling, no CV and no URL of its own.

## What the flat fields in the brief map to

The canonical `Detection` has no `bearing_deg`, `range_m`, `object_class` or `bounding_box`
field; converting to the canonical frame is the adapter's job and those quantities are either
derived, renamed or deliberately not carried. This script therefore reconstructs them for
display, and says which is which:

- **bearing** is recovered by converting the emitted WGS-84 position back into the local ENU
  frame. It is a genuine measurement, just not a stored field.
- **range** is not measured by a fixed monocular camera at all. The position sits at the
  configured `assumed_range_m`, and the printed line says so rather than implying a fix.
- **object_class** is `classification.label`, absent when the model was not confident.
- **bounding_box** never reaches a Detection — a box in pixels is meaningful only to the
  sensor that produced it. It is captured here by wrapping the injected motion detector, which
  is a display device for this script and not a channel into the pipeline.
"""

from __future__ import annotations

import argparse
import math
import sys
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from backend.common.clock import SystemClock
from backend.common.geodesy import GeodeticPoint, LocalFrame
from sensors.adapters.camera.camera_adapter import CameraAdapter
from sensors.adapters.camera.motion_detector import BackgroundSubtractor, MotionRegion
from sensors.adapters.interface import SensorState
from sensors.config.loader import SensorConfig, SensorConfigError, load_sensor_config
from sensors.schemas.detection import Detection

DEFAULT_DURATION_S = 60.0
DEFAULT_INTERVAL_S = 1.5


class LatestFrameSource:
    """A frame source that always hands back the *newest* frame, not the next queued one.

    ## Why this exists

    `VideoCaptureFrameSource` sets `CAP_PROP_BUFFERSIZE` to 1 intending to keep only the
    latest frame. That property is honoured by the DShow and V4L2 backends and **ignored by
    the FFMPEG backend**, which is what serves `http://` MJPEG. So on a network stream the
    setting does nothing and `read()` returns the next frame in sequence.

    The stream produces frames at ~30fps. The adapter consumes one per poll — one every 1.5s
    by default. It therefore falls roughly 44 frames further behind on every poll, and what
    the motion detector sees drifts further from the present with each one: the adapter ends
    up watching the stream in extreme slow motion.

    That is not a tuning problem, and no threshold value fixes it. It makes a bench run
    meaningless in a specific and misleading way — motion is detected long after it happened,
    real motion during the run may never surface before the run ends, and the background model
    is learning a scene minutes in the past.

    ## What this does instead

    Runs a reader thread that pulls frames as fast as the stream delivers them and keeps only
    the most recent one. `read()` returns that. Frames the poll loop never asked for are
    discarded, which is the correct behaviour for a sensor: a frame nobody consumed in time is
    stale data, not a backlog to work through.

    ## Where the real fix belongs

    In `VideoCaptureFrameSource` itself, not here. This class proves the diagnosis and makes
    the bench usable; the adapter is what actually needs draining behaviour so the live
    pipeline does not inherit the same lag. Left as a bench-only workaround for now because
    the adapter is out of scope for this pass.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._capture: cv2.VideoCapture | None = None
        self._latest: np.ndarray | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.frames_read = 0
        self.frames_failed = 0
        self.frames_discarded = 0
        self.last_shape: tuple[int, ...] | None = None
        self.last_mean_brightness: float | None = None

    def open(self) -> bool:
        capture = cv2.VideoCapture(self._url)
        if not capture.isOpened():
            capture.release()
            return False
        self._capture = capture
        self._stop.clear()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return True

    def _pump(self) -> None:
        """Consume the stream continuously, keeping only the newest frame."""
        while not self._stop.is_set():
            capture = self._capture
            if capture is None:
                return
            ok, frame = capture.read()
            if not ok:
                self.frames_failed += 1
                # A failed read on a live stream is usually transient; don't spin hot on it.
                self._stop.wait(0.05)
                continue
            with self._lock:
                if self._latest is not None:
                    self.frames_discarded += 1
                self._latest = frame

    def read(self) -> np.ndarray | None:
        with self._lock:
            frame = self._latest
            self._latest = None
        if frame is None:
            return None
        self.frames_read += 1
        self.last_shape = frame.shape
        self.last_mean_brightness = float(frame.mean())
        return frame

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._capture is not None:
            self._capture.release()
            self._capture = None


class RegionRecordingDetector(BackgroundSubtractor):
    """The real motion detector, remembering what it last returned.

    Subclassed rather than reimplemented so the bench run exercises exactly the detector the
    orchestration loop uses. The recorded boxes are for the terminal only.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.last_regions: list[MotionRegion] = []

    def apply(self, frame: np.ndarray, *, learning_rate: float = -1.0) -> list[MotionRegion]:
        self.last_regions = super().apply(frame, learning_rate=learning_rate)
        return self.last_regions


@dataclass
class RunSummary:
    """What the run did, printed at the end whether it ended by timeout or by Ctrl+C."""

    polls: int = 0
    polls_with_detections: int = 0
    detections: int = 0
    classified: int = 0
    errors: list[str] = field(default_factory=list)


def _bearing_and_offset(
    detection: Detection, config: SensorConfig, frame: LocalFrame
) -> tuple[float, float]:
    """True bearing of a detection, and its horizontal offset from the camera, in metres.

    The offset is printed alongside the assumed range as a check that the geometry round trip
    is sane: it should come back as the configured `assumed_range_m`.
    """
    enu = frame.to_enu(
        GeodeticPoint(
            detection.position.latitude,
            detection.position.longitude,
            detection.position.altitude_m,
        )
    )
    de = enu.east_m - config.camera.position_enu.east_m
    dn = enu.north_m - config.camera.position_enu.north_m
    return math.degrees(math.atan2(de, dn)) % 360.0, math.hypot(de, dn)


def _print_detection(
    index: int,
    detection: Detection,
    region: MotionRegion | None,
    config: SensorConfig,
    frame: LocalFrame,
) -> None:
    bearing, offset_m = _bearing_and_offset(detection, config, frame)
    classification = detection.classification

    box = (
        f"x={region.x} y={region.y} w={region.width} h={region.height} "
        f"({region.area_px} px)"
        if region is not None
        else "unavailable"
    )

    print(f"    [{index}] {detection.detection_id}")
    print(f"        bearing_deg   : {bearing:7.2f}  (measured, recovered from position)")
    print(
        f"        range_m       : not measured — position placed at the configured "
        f"assumed_range_m={config.camera.assumed_range_m:.0f} (round trip {offset_m:.1f} m)"
    )
    print("        velocity_mps  : None (no range, so no honest speed)")
    print(
        f"        object_class  : "
        f"{classification.label.value if classification else 'None (model not confident)'}"
    )
    print(
        f"        confidence    : quality={detection.quality.confidence:.2f}"
        + (f"  classification={classification.confidence:.2f}" if classification else "")
    )
    print(f"        bounding_box  : {box}")
    print(
        f"        position      : {detection.position.latitude:.6f}, "
        f"{detection.position.longitude:.6f}  "
        f"±{detection.measurement.position_uncertainty_m:.0f} m"
    )


def run(duration_s: float, interval_s: float) -> int:
    try:
        config = load_sensor_config()
    except SensorConfigError as exc:
        print(f"FAILED to load sensor config:\n{exc}")
        return 2

    clock = SystemClock()
    frame = LocalFrame(
        GeodeticPoint(
            config.site_origin.latitude,
            config.site_origin.longitude,
            config.site_origin.altitude_m,
        )
    )
    detector = RegionRecordingDetector(
        history=config.camera.motion.background_history,
        var_threshold=config.camera.motion.background_var_threshold,
        min_area_px=config.camera.motion.min_area_px,
    )
    source = LatestFrameSource(config.camera.stream_url)
    adapter = CameraAdapter(
        clock=clock,
        config=config.camera,
        frame=frame,
        frame_source=source,
        motion_detector=detector,
    )

    print("=" * 78)
    print("AETOS — manual camera adapter check")
    print("=" * 78)
    print(f"  sensor_id  : {config.camera.sensor_id}")
    print(f"  stream_url : {config.camera.stream_url}")
    print(f"  min motion : {config.camera.motion.min_area_px} px")
    print(f"  polling    : every {interval_s:.1f}s for up to {duration_s:.0f}s (Ctrl+C to stop)")
    print()

    print("Connecting… (can take up to ~30s if the stream has to retry; let it run)")
    try:
        connected = adapter.connect()
    except KeyboardInterrupt:
        print("\nInterrupted while connecting.")
        adapter.disconnect()
        return 130
    health = adapter.health()

    # OFFLINE immediately after start() means the stream would not open. DEGRADED means it
    # opened but the adapter is not yet at full capability, which is worth running through.
    if health.state is SensorState.OFFLINE:
        print(f"  CONNECT FAILED — {health.reason}")
        print(f"  Nothing at {config.camera.stream_url}. Check that the phone app is running,")
        print("  that this machine is on the same network, and that the address is current.")
        adapter.disconnect()
        return 1

    if connected:
        print(f"  CONNECTED — {health.reason}")
    else:
        # Stream open but not yet fully capable: the background model is still warming up, or
        # the classifier is unavailable. Both are worth running through, not aborting on.
        print(f"  CONNECTED (degraded) — {health.reason}")
    print()

    summary = RunSummary()
    deadline = clock.monotonic() + duration_s

    try:
        while clock.monotonic() < deadline:
            summary.polls += 1
            stamp = clock.now().strftime("%H:%M:%S")

            try:
                detections = adapter.get_detections()
            except Exception as exc:  # noqa: BLE001 - a bench run reports faults, not crashes
                summary.errors.append(f"poll {summary.polls}: {exc}")
                print(f"[{stamp}] poll {summary.polls:3d}  ERROR: {exc}")
                time.sleep(interval_s)
                continue

            if not detections:
                state = adapter.health().state.value
                if source.last_shape is not None:
                    frame_info = (
                        f"frame {source.last_shape[1]}x{source.last_shape[0]}, "
                        f"brightness={source.last_mean_brightness:.0f}"
                    )
                else:
                    frame_info = "no frame decoded yet"
                print(
                    f"[{stamp}] poll {summary.polls:3d}  no motion  "
                    f"({state}, reads={source.frames_read} fails={source.frames_failed}, "
                    f"{frame_info})"
                )
            else:
                summary.polls_with_detections += 1
                summary.detections += len(detections)
                summary.classified += sum(1 for d in detections if d.classification is not None)
                print(f"[{stamp}] poll {summary.polls:3d}  MOTION — {len(detections)} detection(s)")
                regions = detector.last_regions
                for index, detection in enumerate(detections):
                    region = regions[index] if index < len(regions) else None
                    _print_detection(index, detection, region, config, frame)

            time.sleep(interval_s)

    except KeyboardInterrupt:
        print("\nInterrupted.")

    finally:
        adapter.disconnect()
        print("Disconnected.")

    final_health = adapter.health()
    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"  polls                  : {summary.polls}")
    print(f"  polls with detections  : {summary.polls_with_detections}")
    print(f"  detections emitted     : {summary.detections}")
    print(f"  of those, classified   : {summary.classified}")
    print(f"  errors                 : {len(summary.errors)}")
    for error in summary.errors:
        print(f"      {error}")
    print(f"  adapter final state    : {final_health.state.value} — {final_health.reason}")
    print(f"  frames decoded         : {source.frames_read}")
    print(f"  frame reads failed     : {source.frames_failed}")
    print(f"  frames discarded       : {source.frames_discarded}  (stale, never polled)")

    if summary.detections == 0:
        print()
        if source.frames_read == 0:
            print("  No frame was ever decoded — every read() call failed. This is a stream")
            print("  problem, not a motion-detection problem: the background model never saw")
            print("  real pixels, so it could not have detected anything regardless of what")
            print("  was in front of the camera. Check the stream URL in a browser or VLC.")
        else:
            print("  No motion at all, but frames were decoding. Before suspecting the")
            print("  adapter: the background model needs ~15 frames to settle, the moving")
            print(f"  thing has to cover more than {config.camera.motion.min_area_px} px in")
            print("  the frame, and a scene that was already moving when the run started")
            print("  gets learned as background.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--duration", type=float, default=DEFAULT_DURATION_S, help="seconds to run (default 60)"
    )
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL_S, help="seconds between polls"
    )
    args = parser.parse_args()
    return run(args.duration, args.interval)


if __name__ == "__main__":
    sys.exit(main())
