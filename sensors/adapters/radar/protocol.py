"""Frame decoding for a UART target-report radar module. Bytes in, target reports out.

This is the whole of the wire protocol and none of the geometry: it turns a byte stream into
`TargetReport` values expressed exactly as the device expresses them — degrees off boresight,
whole metres of range, kilometres per hour, a signal-quality level with no unit. Converting
any of that into the canonical frame is `adapter.py`'s job, and keeping the two apart is what
lets the decoder be tested against captured bytes with no site, clock or frame in sight.

## The frame

The module emits length-delimited frames of two kinds, distinguished by their header:

    target data:   F4 F3 F2 F1 | length (uint16 LE) | payload | F8 F7 F6 F5
    command reply: FD FC FB FA | length (uint16 LE) | payload | 04 03 02 01

A target-data payload is a one-byte alarm indication, a one-byte target count, and then five
bytes per target:

    byte 0   azimuth, degrees, biased by 0x80 — 0x80 is boresight, 0x7B is 5 degrees to one side
    byte 1   range, whole metres
    byte 2   direction of travel: 0 or 1, one of which is receding (see `approaching_code`)
    byte 3   speed, whole km/h, unsigned — the sign lives in byte 2
    byte 4   signal quality level, unitless, larger is stronger. **Not decibels.**

Two of those need care and are the reason this docstring is long.

**The direction code.** Published descriptions of this device disagree about which code means
approaching, and the example frames in circulation do not settle it. Rather than pick one and
be silently wrong about the sign of every range rate we emit, which would make a departing
object look like a closing one to anything reading radial velocity, the mapping is
configuration (`approaching_code`) and the bench procedure in `docs/hardware_radar_bench.md`
says how to determine it in thirty seconds by walking towards the sensor.

**The quality level.** It is a level, not a ratio in decibels, so it is never written to
`Quality.snr_db`. Putting it there would be a unit lie of exactly the kind FM-004 is about,
and a downstream consumer reading 40 as "40 dB" would be wrong by orders of magnitude.

## Resynchronisation

`FrameDecoder` is a byte-at-a-time state machine rather than a reader that assumes it starts
on a frame boundary, because it does not: a serial port opened mid-transmission delivers the
tail of whatever frame was in flight. On a header whose footer does not check out, one byte is
discarded and the search resumes from the next — never the whole buffer, since a truncated
frame frequently contains the start of the next one. Every discard is counted, and the count
is health information: a link that is resynchronising continuously is miswired or running at
the wrong baud rate, and that is a different fault from a sensor seeing nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

TARGET_FRAME_HEADER = bytes((0xF4, 0xF3, 0xF2, 0xF1))
TARGET_FRAME_FOOTER = bytes((0xF8, 0xF7, 0xF6, 0xF5))
COMMAND_FRAME_HEADER = bytes((0xFD, 0xFC, 0xFB, 0xFA))
COMMAND_FRAME_FOOTER = bytes((0x04, 0x03, 0x02, 0x01))

HEADER_LENGTH = 4
LENGTH_FIELD_LENGTH = 2
FOOTER_LENGTH = 4
PREAMBLE_LENGTH = HEADER_LENGTH + LENGTH_FIELD_LENGTH

TARGET_RECORD_LENGTH = 5
TARGET_HEADER_LENGTH = 2
"""Alarm byte plus target-count byte, ahead of the per-target records."""

AZIMUTH_BIAS = 0x80
"""What the device adds to a signed azimuth to keep the byte unsigned."""

MAX_PAYLOAD_LENGTH = 512
"""Longest payload accepted before the length field is treated as garbage.

The device's own frames are tens of bytes. A length in the thousands means the two bytes read
as a length were not one, and waiting for that many bytes to arrive would stall the decoder
for as long as it took the link to deliver them.
"""

KMH_TO_MPS = 1000.0 / 3600.0


class FrameKind(StrEnum):
    """Which of the two framings a decoded frame arrived in."""

    TARGET_DATA = "target_data"
    COMMAND_REPLY = "command_reply"


@dataclass(frozen=True, slots=True)
class Frame:
    """One complete, footer-verified frame, payload only."""

    kind: FrameKind
    payload: bytes


@dataclass(frozen=True, slots=True)
class TargetReport:
    """One target, in the device's own units and its own frame of reference.

    Deliberately not a `Detection`. This is what the hardware said; a Detection is a statement
    about a position on the Earth, and everything between the two — mounting, tilt, datum,
    uncertainty — is the adapter's responsibility and is applied there.
    """

    azimuth_deg: float
    """Degrees from boresight. Positive and negative sides are as the device reports them;
    which one is clockwise from boresight is a mounting question resolved in the adapter."""

    range_m: float
    """Slant range in metres. Reported to one-metre resolution."""

    speed_mps: float
    """Speed along the line of sight, always non-negative. Unsigned by construction: the
    direction of travel is a separate field, because that is how the device sends it."""

    receding: bool
    """True when the device reports the target moving away from the sensor."""

    signal_level: int
    """Device signal-quality level, unitless, larger is stronger. Not decibels."""

    @property
    def radial_velocity_mps(self) -> float:
        """Range rate under the canonical sign convention: negative towards the sensor.

        Matches `Kinematics.radial_velocity_mps`, which is defined as negative toward and
        positive away, so the conversion happens once here rather than at each use.
        """
        return self.speed_mps if self.receding else -self.speed_mps


@dataclass(frozen=True, slots=True)
class TargetFrame:
    """A decoded target-data payload.

    An empty `targets` list is a real and common message: the device reports on its own
    cadence whether or not anything is in front of it, and those empty reports are how the
    adapter can tell a working sensor observing nothing from a sensor that has gone quiet.
    That distinction is the entire content of FM-010, so the empty case is a value here rather
    than a frame the decoder drops.
    """

    alarm: int
    targets: tuple[TargetReport, ...]


class ProtocolError(ValueError):
    """A frame was well-framed but its contents do not fit the target-data layout."""


@dataclass(slots=True)
class DecodeStats:
    """What the link has been doing, as counts. Read by the adapter's `health()`."""

    frames_decoded: int = 0
    bytes_discarded: int = 0
    """Bytes dropped during resynchronisation, including any preceding a first good frame."""

    malformed_frames: int = 0
    """Frames that framed correctly but whose payload did not parse — a target count that
    disagrees with the payload length, typically. Counted rather than raised past the decoder,
    because one corrupt frame is a fact about the link, not a reason to stop reading it."""


class FrameDecoder:
    """Incremental frame decoder. Feed it whatever the port returned; take out whole frames.

    Holds the partial tail between calls, so the caller may read one byte or four hundred and
    the result is the same. Not thread-safe: one decoder belongs to one reader.
    """

    def __init__(self, max_payload_length: int = MAX_PAYLOAD_LENGTH) -> None:
        self._buffer = bytearray()
        self._max_payload = max_payload_length
        self.stats = DecodeStats()

    def feed(self, data: bytes) -> list[Frame]:
        """Add bytes and return every complete frame they completed, in arrival order."""
        self._buffer.extend(data)
        frames: list[Frame] = []

        while True:
            frame = self._take_one()
            if frame is None:
                return frames
            frames.append(frame)

    def _take_one(self) -> Frame | None:
        while True:
            start = self._find_header()
            if start is None:
                return None
            if start > 0:
                # Everything before a header is either a partial frame we joined mid-flight
                # or noise; either way it cannot be interpreted and is counted as discarded.
                self.stats.bytes_discarded += start
                del self._buffer[:start]

            if len(self._buffer) < PREAMBLE_LENGTH:
                return None

            header = bytes(self._buffer[:HEADER_LENGTH])
            length = int.from_bytes(
                self._buffer[HEADER_LENGTH:PREAMBLE_LENGTH], byteorder="little"
            )

            if length > self._max_payload:
                self._discard_one_byte()
                continue

            total = PREAMBLE_LENGTH + length + FOOTER_LENGTH
            if len(self._buffer) < total:
                return None

            kind, footer = _framing_for(header)
            if bytes(self._buffer[PREAMBLE_LENGTH + length : total]) != footer:
                # The header was a coincidence, or the frame was truncated. Drop one byte
                # only: a mangled frame very often contains the head of the next good one.
                self._discard_one_byte()
                continue

            payload = bytes(self._buffer[PREAMBLE_LENGTH : PREAMBLE_LENGTH + length])
            del self._buffer[:total]
            self.stats.frames_decoded += 1
            return Frame(kind=kind, payload=payload)

    def _find_header(self) -> int | None:
        """Index of the earliest header of either kind, or None if none is present.

        Returns None only when no header *starts* in the buffer. A buffer ending in the first
        three bytes of a header keeps them: they are a partial header, not noise, and
        discarding them would break every frame that happened to straddle a read.
        """
        candidates = [
            index
            for index in (
                self._buffer.find(TARGET_FRAME_HEADER),
                self._buffer.find(COMMAND_FRAME_HEADER),
            )
            if index >= 0
        ]
        if candidates:
            return min(candidates)

        keep = _longest_header_prefix_suffix(self._buffer)
        if keep < len(self._buffer):
            self.stats.bytes_discarded += len(self._buffer) - keep
            del self._buffer[: len(self._buffer) - keep]
        return None

    def _discard_one_byte(self) -> None:
        self.stats.bytes_discarded += 1
        del self._buffer[:1]


def _framing_for(header: bytes) -> tuple[FrameKind, bytes]:
    if header == TARGET_FRAME_HEADER:
        return FrameKind.TARGET_DATA, TARGET_FRAME_FOOTER
    return FrameKind.COMMAND_REPLY, COMMAND_FRAME_FOOTER


def _longest_header_prefix_suffix(buffer: bytearray) -> int:
    """How many trailing bytes could still become a header once more arrive."""
    for size in range(min(HEADER_LENGTH - 1, len(buffer)), 0, -1):
        tail = bytes(buffer[-size:])
        if TARGET_FRAME_HEADER.startswith(tail) or COMMAND_FRAME_HEADER.startswith(tail):
            return size
    return 0


def decode_target_frame(payload: bytes, *, approaching_code: int = 1) -> TargetFrame:
    """Interpret a target-data payload.

    `approaching_code` is the value of the direction byte that means *closing on the sensor*;
    the other value means receding. It is a parameter rather than a constant because the
    device's documentation and the implementations written against it disagree, and the sign
    of every range rate this adapter emits depends on getting it right. See the module
    docstring.
    """
    if approaching_code not in (0, 1):
        raise ValueError(f"approaching_code must be 0 or 1, not {approaching_code}")

    if len(payload) == 0:
        # Observed on real hardware: some units send a genuinely empty payload — header,
        # zero-length field, footer, nothing between — as their "nothing to report" heartbeat,
        # rather than the alarm-byte-plus-zero-count form other units use. Both mean the same
        # thing, and treating one of them as malformed would turn every idle cycle on such a
        # unit into a stream of errors instead of the empty report it actually is (FM-010).
        return TargetFrame(alarm=0, targets=())

    if len(payload) < TARGET_HEADER_LENGTH:
        raise ProtocolError(
            f"target payload is {len(payload)} bytes, too short to carry an alarm byte and a "
            "target count"
        )

    alarm = payload[0]
    count = payload[1]
    expected = TARGET_HEADER_LENGTH + count * TARGET_RECORD_LENGTH
    if len(payload) < expected:
        raise ProtocolError(
            f"target payload claims {count} targets, which needs {expected} bytes, but only "
            f"{len(payload)} arrived"
        )

    targets = tuple(
        _decode_target(
            payload[offset : offset + TARGET_RECORD_LENGTH], approaching_code=approaching_code
        )
        for offset in range(
            TARGET_HEADER_LENGTH, expected, TARGET_RECORD_LENGTH
        )
    )
    return TargetFrame(alarm=alarm, targets=targets)


def _decode_target(record: bytes, *, approaching_code: int) -> TargetReport:
    azimuth_raw, range_m, direction, speed_kmh, signal = record
    return TargetReport(
        azimuth_deg=float(azimuth_raw - AZIMUTH_BIAS),
        range_m=float(range_m),
        speed_mps=speed_kmh * KMH_TO_MPS,
        receding=direction != approaching_code,
        signal_level=signal,
    )


def build_command_frame(command: int, value: bytes = b"") -> bytes:
    """Assemble a command frame. Present for the probe tool, not used in the pipeline.

    The adapter never configures the device while the pipeline is running: a detection range
    or sensitivity that changes underneath a live track is a change to what the sensor is,
    and the operator picture would carry no record of it. Configuration happens deliberately,
    on the bench, through `scripts/radar_probe.py` or the vendor's own tool.
    """
    payload = command.to_bytes(2, byteorder="little") + value
    return (
        COMMAND_FRAME_HEADER
        + len(payload).to_bytes(2, byteorder="little")
        + payload
        + COMMAND_FRAME_FOOTER
    )
