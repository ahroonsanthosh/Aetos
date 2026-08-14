"""`VideoCaptureFrameSource` against a real capture, backed by a generated video file.

The frame-freshness behaviour is the whole point of this class and it is the kind of thing
that silently regresses: the previous implementation looked correct, set the documented
property for it, and still fell further behind the stream on every poll. A test that drives an
actual `cv2.VideoCapture` is the only way to catch that — a mocked capture would happily
confirm whichever behaviour the mock was written to expect.

A file rather than a network stream, because CI has no camera. The two differ in one way that
matters here: a file ends, so the reader thread stops on EOF, while a live stream does not.
Everything these tests assert holds for both.
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from sensors.adapters.camera.camera_adapter import VideoCaptureFrameSource

FRAME_COUNT = 120
FRAME_WIDTH = 320
FRAME_HEIGHT = 240


@pytest.fixture(scope="module")
def video_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A short clip whose frames are individually identifiable by their brightness."""
    path = tmp_path_factory.mktemp("capture") / "clip.avi"
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30, (FRAME_WIDTH, FRAME_HEIGHT)
    )
    try:
        for index in range(FRAME_COUNT):
            # Brightness rises frame by frame, so a frame's value identifies its position in
            # the clip and a test can tell "the newest frame" from "the next queued one".
            value = 20 + index
            writer.write(np.full((FRAME_HEIGHT, FRAME_WIDTH, 3), value, dtype=np.uint8))
    finally:
        writer.release()
    return path


_POLL_INTERVAL_S = 0.01


def _await_frame(source: VideoCaptureFrameSource, timeout_s: float = 5.0) -> np.ndarray:
    """Poll until the reader thread has produced a frame. Fails rather than hanging.

    Counts attempts rather than reading a wall clock: ADR 0003 keeps direct time calls out of
    `sensors/`, and a bounded attempt count is a sufficient timeout for a test.
    """
    for _ in range(int(timeout_s / _POLL_INTERVAL_S)):
        frame = source.read()
        if frame is not None:
            return frame
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"no frame within {timeout_s}s")


def test_open_reports_failure_for_an_unusable_source() -> None:
    """An unreachable stream is False, not an exception and not a hang."""
    source = VideoCaptureFrameSource("/nonexistent/path/does/not/exist.avi")
    try:
        assert source.open() is False
    finally:
        source.close()


def test_reads_frames_from_the_stream(video_file: Path) -> None:
    source = VideoCaptureFrameSource(str(video_file))
    assert source.open() is True
    try:
        frame = _await_frame(source)
        assert frame.shape == (FRAME_HEIGHT, FRAME_WIDTH, 3)
    finally:
        source.close()


def test_read_returns_none_rather_than_repeating_a_consumed_frame(video_file: Path) -> None:
    """Silence must stay silent.

    `CameraAdapter` decides a stream has stalled by ageing its last successful read. If a
    consumed frame were handed back again, a dead stream would look like a still scene forever
    and the adapter would never report DEGRADED (FM-010).
    """
    source = VideoCaptureFrameSource(str(video_file))
    assert source.open() is True
    try:
        _await_frame(source)
        # Immediately after consuming, before the reader can plausibly have delivered another.
        assert source.read() is None
    finally:
        source.close()


def test_the_reader_outpaces_a_slow_poll_loop(video_file: Path) -> None:
    """The behaviour the whole class exists for.

    A slow consumer must not drag the stream along behind it. After a pause far longer than
    the frame interval, the frame handed back is one the reader took recently — not the one
    that happened to be next in the queue when the pause began.
    """
    source = VideoCaptureFrameSource(str(video_file))
    assert source.open() is True
    try:
        first = _await_frame(source)
        first_value = int(first[0, 0, 0])

        # Long enough for the reader to have consumed many frames, none of them polled.
        time.sleep(0.5)

        latest = _await_frame(source)
        latest_value = int(latest[0, 0, 0])

        assert latest_value > first_value + 1, (
            "the second poll returned the frame immediately after the first, so the reader is "
            "being consumed at the poll rate rather than the stream rate"
        )
        assert source.frames_dropped > 0, "no frames were superseded during a 0.5s pause"
    finally:
        source.close()


def test_close_is_idempotent_and_stops_the_reader(video_file: Path) -> None:
    source = VideoCaptureFrameSource(str(video_file))
    assert source.open() is True
    _await_frame(source)

    source.close()
    source.close()

    assert source.read() is None


def test_counters_reset_on_reopen(video_file: Path) -> None:
    """A restart describes the new run. Counts surviving it would describe a run that ended."""
    source = VideoCaptureFrameSource(str(video_file))
    assert source.open() is True
    _await_frame(source)
    time.sleep(0.2)
    source.close()
    assert source.frames_received > 0

    assert source.open() is True
    try:
        assert source.frames_dropped == 0
        assert source.read_failures == 0
    finally:
        source.close()
