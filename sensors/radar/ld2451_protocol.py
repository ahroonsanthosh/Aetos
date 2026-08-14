"""Byte-level protocol for the LD2451 testbed unit: framing, target decoding, config commands.

Everything in this module is specific to one model of radar. It is the layer that gets deleted
when the unit is replaced, and it is kept separate from the driver so that "replace the
hardware" means replacing two files rather than picking vendor constants out of connection
management and health reporting.

## The byte values here are unverified

The frame headers, tails, command words and field layouts below are drawn from secondhand
descriptions of the vendor's protocol document, not from the document itself. The first-run
checklist in `docs/radar_testbed.md` requires them to be checked against the vendor PDF before
any detection logic is built on top, and they are gathered into named constants at the top of
this file specifically so that verifying them is reading one screen rather than auditing a
parser.

The decoder is written to fail *loudly and countably* if they are wrong. A frame with a bad
tail is discarded and counted, not repaired; bytes dropped during resynchronisation are counted
separately. A wrong baud rate or a wrong header value therefore shows up as a rising
`malformed_frames` or a flood of `bytes_discarded` with no decodes — a distinctive signature —
rather than as plausible-looking targets at nonsense ranges.

## Endianness

Multi-byte fields are little-endian, and `int.from_bytes(..., "little")` is written out at every
site rather than wrapped in a helper, so that the one assumption most likely to be wrong is
visible wherever it is made.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MODEL_DESIGNATION = "LD2451"
"""What this unit calls itself. Carried into `TargetDetection.raw_source` and the raw log as
data — never read to decide behaviour anywhere."""

# --------------------------------------------------------------------------------------------
# Framing constants. UNVERIFIED against the vendor document — see the module docstring.
# --------------------------------------------------------------------------------------------

DATA_FRAME_HEADER = bytes((0xF4, 0xF3, 0xF2, 0xF1))
DATA_FRAME_TAIL = bytes((0xF8, 0xF7, 0xF6, 0xF5))

COMMAND_FRAME_HEADER = bytes((0xFD, 0xFC, 0xFB, 0xFA))
COMMAND_FRAME_TAIL = bytes((0x04, 0x03, 0x02, 0x01))

HEADER_LENGTH = 4
TAIL_LENGTH = 4
LENGTH_FIELD_LENGTH = 2

MAX_PAYLOAD_BYTES = 512
"""Ceiling on a declared payload length. A length field larger than this is treated as a
malformed frame rather than as a reason to wait for half a kilobyte that will never arrive —
which is how a decoder fed noise ends up permanently stalled instead of visibly failing."""

MAX_BUFFER_BYTES = 8192
"""Ceiling on retained unparsed bytes. Reached only when the stream is not this protocol at
all; the decoder then drops the oldest bytes so that memory stays bounded and the discard
counter climbs, which is the evidence that says so."""

# --------------------------------------------------------------------------------------------
# Target report layout. UNVERIFIED — see the module docstring.
# --------------------------------------------------------------------------------------------

TARGET_HEADER_BYTES = 2
"""Target count, then one byte of alarm state, ahead of the per-target blocks."""

TARGET_BLOCK_BYTES = 5
"""angle, distance, direction, speed, signal level — one byte each."""

ANGLE_ZERO_OFFSET = 0x80
"""The angle byte is unsigned with zero at 0x80, so a reported angle is `raw - 0x80` degrees."""

DIRECTION_RECEDING = 0x00
DIRECTION_APPROACHING = 0x01

KMH_TO_MPS = 1000.0 / 3600.0

# --------------------------------------------------------------------------------------------
# Configuration command words. UNVERIFIED — see the module docstring.
# --------------------------------------------------------------------------------------------

CMD_ENABLE_CONFIG = 0x00FF
CMD_DISABLE_CONFIG = 0x00FE
CMD_SET_TARGET_DETECTION = 0x0002
CMD_SET_SENSITIVITY = 0x0003
CMD_READ_FIRMWARE_VERSION = 0x00A0

ENABLE_CONFIG_VALUE = bytes((0x01, 0x00))
"""Payload the enable-config command carries. The unit ignores every other configuration
command until it has been sent, and returns to normal reporting when config is disabled."""

COMMAND_REPLY_FLAG = 0x0100
"""A reply's command word is the request's word with this bit set."""

COMMAND_STATUS_SUCCESS = 0x0000


class FrameKind(StrEnum):
    """Which of the unit's two frame types this is.

    Both share a length-delimited shape and differ only in their header and tail bytes, so one
    decoder handles both and labels what it found. Keeping them in one stream matters: a
    configuration reply arrives interleaved with target data, and a decoder that only knew
    about one of them would count the other as noise.
    """

    DATA = "data"
    COMMAND_REPLY = "command_reply"


@dataclass(frozen=True, slots=True)
class Frame:
    """One well-formed frame: what kind it was, and the payload between length and tail."""

    kind: FrameKind
    payload: bytes


@dataclass(frozen=True, slots=True)
class TargetReport:
    """One target as the unit describes it, before any interpretation.

    Deliberately still in the unit's own units and conventions — degrees off boresight, whole
    metres, kilometres per hour, an uncalibrated signal index. Converting to SI and to the
    hardware-agnostic `TargetDetection` is the driver's job, and keeping the two apart is what
    makes it possible to check a decode against a hex dump of the wire.
    """

    angle_deg: float
    distance_m: float
    approaching: bool
    speed_kmh: float
    signal_level: int
    """Uncalibrated signal-strength index, unit-defined. Carried to the raw log and dropped at
    the driver boundary: it is not a probability, not a signal-to-noise ratio in decibels, and
    rescaling it into either would be inventing a measurement."""

    @property
    def radial_velocity_mps(self) -> float:
        """Range rate in metres per second, positive receding.

        The unit reports an unsigned speed and a separate direction flag; the canonical
        convention downstream is a signed range rate, so the sign is applied once, here, rather
        than being carried as a second field that some later caller forgets to consult.
        """
        magnitude = self.speed_kmh * KMH_TO_MPS
        return -magnitude if self.approaching else magnitude


@dataclass(frozen=True, slots=True)
class CommandReply:
    """The unit's answer to a configuration command."""

    command_word: int
    status: int
    payload: bytes

    @property
    def succeeded(self) -> bool:
        return self.status == COMMAND_STATUS_SUCCESS


class ProtocolError(ValueError):
    """A frame was well-formed at the framing level but not at the payload level.

    Separate from a framing failure because the two mean different things: a bad tail says the
    byte stream is not synchronised, whereas a target block that is three bytes short says the
    framing is right and the *layout* assumption is wrong. Conflating them would hide the second
    diagnosis behind the first.
    """


class FrameDecoder:
    """Incremental frame extraction from a serial byte stream.

    Serial reads have no relationship to frame boundaries: one read may carry three frames and
    half of a fourth, the next may carry the other half. The decoder therefore owns a buffer
    across calls and returns only frames it has seen in full.

    ## Resynchronisation

    On startup, after a dropped link, and whenever the unit is mid-frame when we begin reading,
    the buffer starts inside a frame rather than at one. `feed()` hunts forward for the nearest
    header of either kind and discards everything before it, counting the discarded bytes.

    Discarding is the right response and counting it is what makes it safe: a decoder that
    silently skipped would make a permanently misframed stream — the signature of a wrong baud
    rate — look identical to a quiet radar.
    """

    __slots__ = ("_buffer", "_bytes_discarded", "_malformed_frames")

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._bytes_discarded = 0
        self._malformed_frames = 0

    @property
    def bytes_discarded(self) -> int:
        return self._bytes_discarded

    @property
    def malformed_frames(self) -> int:
        return self._malformed_frames

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        """Drop buffered bytes without touching the counters.

        Called on reconnect. The half-frame left over from a link that died is not part of any
        frame the reopened link will send, and keeping it would corrupt the first real frame
        after recovery. The counters survive because they describe the session, not the buffer.
        """
        self._bytes_discarded += len(self._buffer)
        self._buffer.clear()

    def feed(self, data: bytes) -> list[Frame]:
        """Add bytes and return every complete frame now available, in arrival order."""
        if data:
            self._buffer.extend(data)
        self._enforce_buffer_ceiling()

        frames: list[Frame] = []
        while True:
            frame = self._take_one()
            if frame is None:
                return frames
            frames.append(frame)

    def _enforce_buffer_ceiling(self) -> None:
        """Bound memory when the stream never frames.

        Only reachable when nothing in the buffer parses, which in practice means the link is
        carrying something other than this protocol. Dropping the oldest bytes keeps the newest
        — the ones most likely to contain a header if the stream ever recovers.
        """
        excess = len(self._buffer) - MAX_BUFFER_BYTES
        if excess > 0:
            del self._buffer[:excess]
            self._bytes_discarded += excess

    def _take_one(self) -> Frame | None:
        """Extract the next complete frame, or None if the buffer does not hold one yet."""
        while True:
            start, kind = self._find_header()
            if start is None or kind is None:
                return None

            if start > 0:
                del self._buffer[:start]
                self._bytes_discarded += start

            if len(self._buffer) < HEADER_LENGTH + LENGTH_FIELD_LENGTH:
                return None

            declared = int.from_bytes(
                self._buffer[HEADER_LENGTH : HEADER_LENGTH + LENGTH_FIELD_LENGTH], "little"
            )
            if declared > MAX_PAYLOAD_BYTES:
                # An absurd length is a misread header, not a giant frame. Step past this
                # header byte and hunt again rather than waiting for bytes that never come.
                self._discard_leading_byte()
                self._malformed_frames += 1
                continue

            total = HEADER_LENGTH + LENGTH_FIELD_LENGTH + declared + TAIL_LENGTH
            if len(self._buffer) < total:
                return None

            tail = bytes(self._buffer[total - TAIL_LENGTH : total])
            expected_tail = (
                DATA_FRAME_TAIL if kind is FrameKind.DATA else COMMAND_FRAME_TAIL
            )
            if tail != expected_tail:
                self._discard_leading_byte()
                self._malformed_frames += 1
                continue

            payload = bytes(self._buffer[HEADER_LENGTH + LENGTH_FIELD_LENGTH : total - TAIL_LENGTH])
            del self._buffer[:total]
            return Frame(kind=kind, payload=payload)

    def _find_header(self) -> tuple[int | None, FrameKind | None]:
        """Offset and kind of the earliest header in the buffer, or (None, None).

        When neither header is present the buffer may still end with a partial one, so the last
        three bytes are retained rather than discarded — otherwise a header split across two
        serial reads would be destroyed by the very step meant to find it.
        """
        data_at = self._buffer.find(DATA_FRAME_HEADER)
        command_at = self._buffer.find(COMMAND_FRAME_HEADER)

        candidates = [(at, kind) for at, kind in
                      ((data_at, FrameKind.DATA), (command_at, FrameKind.COMMAND_REPLY))
                      if at >= 0]
        if candidates:
            return min(candidates)

        keep = HEADER_LENGTH - 1
        if len(self._buffer) > keep:
            dropped = len(self._buffer) - keep
            del self._buffer[:dropped]
            self._bytes_discarded += dropped
        return None, None

    def _discard_leading_byte(self) -> None:
        """Step one byte past a header that did not lead to a valid frame.

        One byte, not four: the real header may begin inside what we mistook for one, and
        skipping the whole thing would step over it.
        """
        if self._buffer:
            del self._buffer[:1]
            self._bytes_discarded += 1


def parse_target_frame(payload: bytes) -> list[TargetReport]:
    """Decode a data frame's payload into target reports.

    Raises `ProtocolError` when the payload does not match the documented layout. The caller
    counts and discards; it does not attempt a partial decode, because a payload whose length
    disagrees with its own target count is a payload whose field offsets cannot be trusted, and
    half-decoding it would produce targets at fabricated ranges.
    """
    if len(payload) < TARGET_HEADER_BYTES:
        raise ProtocolError(
            f"target frame payload is {len(payload)} bytes, too short to carry a target count"
        )

    target_count = payload[0]
    expected = TARGET_HEADER_BYTES + target_count * TARGET_BLOCK_BYTES
    if len(payload) != expected:
        raise ProtocolError(
            f"target frame declares {target_count} target(s), which needs {expected} payload "
            f"bytes, but the frame carried {len(payload)}"
        )

    reports: list[TargetReport] = []
    for index in range(target_count):
        offset = TARGET_HEADER_BYTES + index * TARGET_BLOCK_BYTES
        angle_raw, distance_raw, direction_raw, speed_raw, signal_raw = payload[
            offset : offset + TARGET_BLOCK_BYTES
        ]
        reports.append(
            TargetReport(
                angle_deg=float(angle_raw - ANGLE_ZERO_OFFSET),
                distance_m=float(distance_raw),
                approaching=direction_raw == DIRECTION_APPROACHING,
                speed_kmh=float(speed_raw),
                signal_level=int(signal_raw),
            )
        )
    return reports


def parse_command_reply(payload: bytes) -> CommandReply:
    """Decode a command frame's payload into the unit's answer."""
    if len(payload) < 4:
        raise ProtocolError(
            f"command reply payload is {len(payload)} bytes, too short to carry a command word "
            f"and status"
        )
    command_word = int.from_bytes(payload[0:2], "little")
    status = int.from_bytes(payload[2:4], "little")
    return CommandReply(command_word=command_word, status=status, payload=payload[4:])


def build_command(command_word: int, value: bytes = b"") -> bytes:
    """Wrap a command word and its value in a complete command frame."""
    body = command_word.to_bytes(2, "little") + value
    return (
        COMMAND_FRAME_HEADER
        + len(body).to_bytes(LENGTH_FIELD_LENGTH, "little")
        + body
        + COMMAND_FRAME_TAIL
    )


def enable_config_command() -> bytes:
    """The handshake that must precede every other configuration command."""
    return build_command(CMD_ENABLE_CONFIG, ENABLE_CONFIG_VALUE)


def disable_config_command() -> bytes:
    """Leave configuration mode and resume reporting. Sent even after a failed command, so a
    unit is never left in a mode where it reports nothing because a setting was rejected."""
    return build_command(CMD_DISABLE_CONFIG)


def set_target_detection_command(
    *, max_distance_m: int, min_speed_kmh: int, no_target_delay_s: int, direction_filter: int = 2
) -> bytes:
    """Detection distance, minimum radial speed, hold time, and which directions to report.

    `direction_filter` defaults to 2 — report both approaching and receding targets. Filtering
    by direction is available and deliberately not used: a receding airframe is still an
    airframe, and discarding half the returns at the firmware level would remove evidence the
    tracker needs to hold a track through a turn.
    """
    value = bytes(
        (
            max_distance_m & 0xFF,
            direction_filter & 0xFF,
            min_speed_kmh & 0xFF,
            no_target_delay_s & 0xFF,
        )
    )
    return build_command(CMD_SET_TARGET_DETECTION, value)


def set_sensitivity_command(*, min_confirm_frames: int, sensitivity: int) -> bytes:
    """Consecutive-detection threshold and detection sensitivity.

    Both are exposed as tunables all the way up to `RadarSensor.configure()` because this unit
    ships tuned for vehicle-scale returns and its performance against the intended target is
    unmeasured. Freezing either at a default would be a guess dressed as a decision.
    """
    value = bytes((min_confirm_frames & 0xFF, sensitivity & 0xFF, 0x00, 0x00))
    return build_command(CMD_SET_SENSITIVITY, value)


def read_firmware_version_command() -> bytes:
    """Ask the unit to identify itself.

    Used as the connection probe: it is the cheapest command whose reply proves the link is
    both open and correctly framed, which is precisely the question an unconfirmed baud rate
    leaves open.
    """
    return build_command(CMD_READ_FIRMWARE_VERSION)
