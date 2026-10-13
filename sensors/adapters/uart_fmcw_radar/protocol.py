"""Byte-level protocol for the serial-attached FMCW radar: framing, decoding, commands.

Pure functions and a stateful byte-stream parser. No I/O, no clock, no canonical schema — the
output of this module is what the *sensor* said, in the sensor's own units and its own
sensor-relative frame. Turning that into a canonical `Detection` is `adapter.py`'s job, and
keeping the two apart is what makes the frame layout testable against a captured byte log.

## Framing

Two frame families share the link, distinguished by their magic words:

    report   F4 F3 F2 F1 | len(LE16) | payload | F8 F7 F6 F5
    command  FD FC FB FA | len(LE16) | payload | 04 03 02 01

All multi-byte fields are little-endian.

## What is verified and what is not

The framing constants and the little-endian convention are consistent across every source and
across the vendor's other units in this family. The **report payload layout is not fully
settled**: sources differ over whether the payload opens with one count byte or with an alarm
byte followed by a count. Rather than pick one and be silently wrong, `parse_report_payload`
accepts both when the declared length makes the reading unambiguous, and records which layout
it saw. Confirm against the vendor's protocol PDF and then delete the branch that is wrong —
that deletion is a task, not an option (see this package's README).

## Robustness

A malformed frame is discarded, never raised past the parser: the byte stream from a serial
link is not a trusted input, and an adapter that dies on a corrupt frame is a sensor that goes
silent the first time someone brushes a jumper wire. Discards are counted, and a stream that
is *only* discards is what health reporting uses to distinguish a wrong baud rate from a quiet
sky.
"""

from __future__ import annotations

from dataclasses import dataclass

REPORT_HEADER = b"\xf4\xf3\xf2\xf1"
REPORT_TAIL = b"\xf8\xf7\xf6\xf5"
COMMAND_HEADER = b"\xfd\xfc\xfb\xfa"
COMMAND_TAIL = b"\x04\x03\x02\x01"

_LENGTH_BYTES = 2
_TARGET_RECORD_BYTES = 5
_AZIMUTH_ZERO_OFFSET = 0x80
"""The unit reports azimuth as an unsigned byte biased by 0x80, so 0x80 is boresight."""

_KMH_TO_MPS = 1000.0 / 3600.0

MAX_PAYLOAD_BYTES = 1024
"""A declared length beyond this is corruption, not a large frame. Bounds how far the parser
will buffer before giving up on a candidate header."""


class FrameError(ValueError):
    """A frame was structurally invalid. Raised only by the pure decoders, which the
    stream parser calls behind a `try` — it never escapes `ReportParser.feed`."""


@dataclass(frozen=True, slots=True)
class RadarTarget:
    """One target as the sensor described it, in the sensor's own terms.

    Sensor-relative and unconverted on purpose. Azimuth is relative to the PCB, not to north;
    speed is a magnitude with a separate direction flag, not a signed range rate; quality is
    an uncalibrated integer, not decibels. Everything this class does not know about — where
    the sensor is pointing, what the mount tilt is, what the numbers mean in WGS-84 — is
    deliberately absent, because that is the boundary that lets a captured byte log be replayed
    through this parser with no site configured.
    """

    azimuth_deg: float
    """Signed, relative to boresight. Negative is left of the centreline."""

    range_m: float
    approaching: bool
    """True when the unit flags the target as closing on the sensor."""

    speed_kmh: int
    """Magnitude only. The sensor reports no sign; `approaching` carries the direction."""

    quality: int
    """Uncalibrated signal-quality byte, 0-255. Not an SNR in decibels — see
    `ConfidenceConfig` for why it is never emitted as one."""

    @property
    def radial_velocity_mps(self) -> float:
        """Range rate in the canonical sign convention: positive away, negative closing."""
        magnitude = self.speed_kmh * _KMH_TO_MPS
        return -magnitude if self.approaching else magnitude


@dataclass(frozen=True, slots=True)
class RadarReport:
    """One decoded report frame."""

    targets: tuple[RadarTarget, ...]
    alarm: int | None = None
    """Alarm/status byte when the payload carried one, else None. Unused downstream; retained
    because a raw log is only worth keeping if nothing was thrown away decoding it."""


@dataclass(slots=True)
class ParserStats:
    """What the byte stream has been doing, for health reporting.

    `bytes_discarded` is the interesting one. A link at the wrong baud rate produces a stream
    that never syncs, and without this counter that is indistinguishable from a sensor that
    sees nothing — the exact confusion FM-010 is about, arriving one layer lower down.
    """

    frames_decoded: int = 0
    frames_malformed: int = 0
    bytes_discarded: int = 0
    consecutive_bytes_discarded: int = 0


def decode_azimuth(raw: int) -> float:
    """Biased azimuth byte to signed degrees from boresight."""
    return float(raw - _AZIMUTH_ZERO_OFFSET)


def parse_report_payload(payload: bytes) -> RadarReport:
    """Decode a report frame's payload.

    Accepts the two candidate layouts described in the module docstring, preferring the
    count-first reading and falling back to alarm-then-count only when the length rules the
    first out. Anything that matches neither is a `FrameError`.
    """
    if not payload:
        raise FrameError("empty report payload")

    count = payload[0]
    if len(payload) == 1 + count * _TARGET_RECORD_BYTES:
        return RadarReport(targets=_parse_targets(payload[1:], count))

    if len(payload) >= 2:
        count = payload[1]
        if len(payload) == 2 + count * _TARGET_RECORD_BYTES:
            return RadarReport(targets=_parse_targets(payload[2:], count), alarm=payload[0])

    raise FrameError(f"report payload length {len(payload)} matches no known target-record layout")


def _parse_targets(body: bytes, count: int) -> tuple[RadarTarget, ...]:
    targets = []
    for index in range(count):
        offset = index * _TARGET_RECORD_BYTES
        azimuth, distance, direction, speed, quality = body[offset : offset + _TARGET_RECORD_BYTES]
        targets.append(
            RadarTarget(
                azimuth_deg=decode_azimuth(azimuth),
                range_m=float(distance),
                approaching=direction == 1,
                speed_kmh=speed,
                quality=quality,
            )
        )
    return tuple(targets)


def build_command(command_word: int, value: bytes = b"") -> bytes:
    """A framed command, ready to write.

    Commands are only accepted by the unit between `enable configuration` and `end
    configuration`; `adapter.configure()` owns that handshake so that no caller can leave the
    sensor parked in configuration mode, which is a state in which it reports nothing at all.
    """
    payload = command_word.to_bytes(2, "little") + value
    return COMMAND_HEADER + len(payload).to_bytes(_LENGTH_BYTES, "little") + payload + COMMAND_TAIL


class Command:
    """Command words, from the vendor protocol description and UNVERIFIED against the official
    PDF. The unit acknowledges each command with a frame of its own; this driver does not yet
    read those acknowledgements back, so a `configure()` call that returns has been *sent*, not
    confirmed. Verifying the acknowledgement is on the bring-up checklist in the README."""

    ENABLE_CONFIG = 0x00FF
    END_CONFIG = 0x00FE
    SET_DETECTION_PARAMS = 0x0002
    SET_SENSITIVITY = 0x0003


class ReportParser:
    """Stateful byte-stream framer. Feed it whatever arrived; take back whole reports.

    Serial reads split frames anywhere, so the parser buffers across calls and resynchronises
    by searching for the header magic. Bytes skipped during a resync are counted rather than
    silently dropped.
    """

    __slots__ = ("_buffer", "stats")

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.stats = ParserStats()

    def feed(self, data: bytes) -> list[RadarReport]:
        """Consume bytes, returning one `RadarReport` per complete, valid frame.

        Raw bytes are not returned: the adapter logs the read chunk exactly as it arrived,
        which captures the noise and the malformed frames too — and those are the ones worth
        having when a session has to be explained afterwards.
        """
        self._buffer.extend(data)
        decoded: list[RadarReport] = []

        while True:
            start = self._buffer.find(REPORT_HEADER)
            if start < 0:
                # Keep only what could still be the leading bytes of a header.
                keep = len(REPORT_HEADER) - 1
                self._discard(max(0, len(self._buffer) - keep))
                return decoded
            if start > 0:
                self._discard(start)

            before = len(self._buffer)
            frame = self._take_frame()
            if frame is None:
                if len(self._buffer) == before:
                    # Nothing consumed: the frame is incomplete, so wait for more bytes.
                    return decoded
                # A byte was dropped from a bad candidate header — resynchronise.
                continue

            _raw, payload = frame
            try:
                report = parse_report_payload(payload)
            except (FrameError, IndexError):
                self.stats.frames_malformed += 1
                continue
            self.stats.frames_decoded += 1
            self.stats.consecutive_bytes_discarded = 0
            decoded.append(report)

    def _take_frame(self) -> tuple[bytes, bytes] | None:
        """Pull one complete frame off the front of the buffer, if one is there.

        Assumes the buffer starts at a header match. Returns None when more bytes are needed,
        or when the candidate was noise. A frame whose declared length is impossible, or whose
        tail is missing where the length says it should be, is not a frame: the header was
        noise that happened to match, so one byte is dropped and the outer search resumes
        rather than trusting the length field of a frame we have already caught lying.
        """
        header_end = len(REPORT_HEADER) + _LENGTH_BYTES
        if len(self._buffer) < header_end:
            return None

        length = int.from_bytes(self._buffer[len(REPORT_HEADER) : header_end], "little")
        if length > MAX_PAYLOAD_BYTES:
            self.stats.frames_malformed += 1
            self._discard(1)
            return None

        end = header_end + length + len(REPORT_TAIL)
        if len(self._buffer) < end:
            return None

        if bytes(self._buffer[end - len(REPORT_TAIL) : end]) != REPORT_TAIL:
            self.stats.frames_malformed += 1
            self._discard(1)
            return None

        raw = bytes(self._buffer[:end])
        payload = bytes(self._buffer[header_end : header_end + length])
        del self._buffer[:end]
        return raw, payload

    def _discard(self, count: int) -> None:
        if count <= 0:
            return
        del self._buffer[:count]
        self.stats.bytes_discarded += count
        self.stats.consecutive_bytes_discarded += count

    def reset(self) -> None:
        """Drop buffered bytes. Called on reconnect: bytes from before a link drop cannot be
        assumed to join up with bytes from after it."""
        self._buffer.clear()
