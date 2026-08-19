"""Wire protocol for the UART FMCW radar's data-report frames.

Pure parsing, no I/O and no `pyserial` dependency — this module can be unit-tested with
hand-built byte sequences and never needs a serial port. Frame layout reverse-derived from a
community reference implementation of the vendor's serial protocol (cited in this package's
README); it has not been independently verified against the vendor's own protocol document,
and every field whose interpretation is an assumption rather than a confirmed fact says so
below and in `config.py`.

Frame shape::

    [DATA_FRAME_HEADER: 4B] [payload_len: u16 little-endian] [payload: payload_len B]
    [DATA_FRAME_FOOTER: 4B]

Payload shape::

    [target_count: 1B] [reserved: 1B] [target record: 5B]...

Target record shape (5 bytes)::

    [angle: 1B] [distance_m: 1B] [direction: 1B] [speed_kph: 1B] [signal: 1B]

This module only recognises the shape of a frame and the raw fields inside it. Assigning
meaning to those fields in Aetos's own terms — sign conventions, coordinate frames, dropped
versus kept — is `adapter.py`'s job, driven by `config.RadarMountConfig`.
"""

from __future__ import annotations

from dataclasses import dataclass

DATA_FRAME_HEADER = b"\xf4\xf3\xf2\xf1"
DATA_FRAME_FOOTER = b"\xf8\xf7\xf6\xf5"

CONFIG_FRAME_HEADER = b"\xfd\xfc\xfb\xfa"
CONFIG_FRAME_FOOTER = b"\x04\x03\x02\x01"
"""Framing for the module's configuration/ack channel. Present so a config-frame byte sequence
is at least recognisable as *not* a data frame; this adapter never sends configuration commands
and does not parse config-frame payloads. See the package README, "Never configures the
module"."""

_HEADER_LEN = 4
_LENGTH_FIELD_LEN = 2
_FOOTER_LEN = 4
_PAYLOAD_PREFIX_LEN = 2
"""`target_count` and a reserved byte, ahead of the target records in every payload."""

_TARGET_RECORD_LEN = 5
_ANGLE_ZERO_OFFSET = 0x80

MAX_PLAUSIBLE_PAYLOAD_LEN = 256
"""Ceiling on a declared payload length before the header is treated as noise.

The length field is two bytes, so a corrupt one can declare up to 65535. Believing it means
withholding every subsequent frame until that many bytes have arrived — several seconds of
apparent silence at this link's baud rate, which is exactly the "empty sky versus broken
sensor" ambiguity FM-010 is about. A real payload here is a 2-byte prefix plus a handful of
5-byte records, so anything past this is a corrupt header rather than a large frame."""


class FrameError(ValueError):
    """A byte sequence framed correctly but did not parse as a valid payload."""


@dataclass(frozen=True, slots=True)
class RawTarget:
    """One target record, still in the sensor's raw units and raw wire encoding.

    Not a `Detection` — no geometry, no uncertainty model, no Aetos sign convention has been
    applied yet. That conversion happens in `adapter.py`, using `RadarMountConfig`.
    """

    angle_deg: float
    """Signed offset from the sensor's boresight (`byte - 0x80`). Which side of boresight is
    positive is an assumption, not a confirmed convention — see the package README."""

    distance_m: float
    """Raw distance byte, taken as whole metres."""

    direction_byte: int
    """0x00 or non-zero, exactly as read off the wire. Which value means "approaching" is
    UNVERIFIED against the vendor's own protocol document — see
    `config.RadarMountConfig.direction_zero_means_approaching`."""

    speed_kph: float
    """Unsigned magnitude. Sign and unit conversion to Aetos's `radial_velocity_mps` happen in
    `adapter.py`."""

    signal_raw: int
    """The module's per-target signal-strength byte, 0-255. Units are undocumented in the
    sources available when this adapter was written; treated only as a relative, monotonic
    proxy for return strength, never as a calibrated SNR in dB."""


@dataclass(frozen=True, slots=True)
class FrameScan:
    """The result of one pass over a receive buffer."""

    payloads: list[bytes]
    """Complete data-frame payloads, in wire order."""

    remainder: bytes
    """Bytes not yet consumed — a partial frame awaiting the rest of its bytes, or the tail
    that might hold the start of a header split across two reads."""

    resyncs: int
    """How many header-shaped byte sequences did not lead to a valid footer and were skipped.

    Reported rather than silently swallowed because this is the only observable symptom of a
    marginal link — corrupt framing that still parses is invisible otherwise, and an adapter
    cannot report a link as degraded on evidence it threw away."""


def extract_data_frames(buffer: bytes) -> FrameScan:
    """Every complete data-frame payload found in `buffer`, plus what could not be consumed.

    Scans for `DATA_FRAME_HEADER` rather than assuming the buffer starts aligned, because a
    serial link can be opened mid-stream or lose framing after line noise. A header without a
    complete frame behind it yet is left in the remainder for the next call; a header whose
    declared length is implausible or does not lead to a valid footer is treated as a false
    match inside noise and skipped by one byte, rather than trusted. Never raises — losing and
    regaining framing is normal behaviour for a live UART link, not a caller error.
    """
    payloads: list[bytes] = []
    resyncs = 0
    cursor = 0
    while True:
        start = buffer.find(DATA_FRAME_HEADER, cursor)
        if start == -1:
            # Keep the last few bytes in case a header is split across this call and the next.
            tail_start = max(cursor, len(buffer) - (_HEADER_LEN - 1))
            return FrameScan(payloads, buffer[tail_start:], resyncs)

        length_start = start + _HEADER_LEN
        length_end = length_start + _LENGTH_FIELD_LEN
        if length_end > len(buffer):
            return FrameScan(payloads, buffer[start:], resyncs)

        payload_len = int.from_bytes(buffer[length_start:length_end], byteorder="little")
        if payload_len > MAX_PLAUSIBLE_PAYLOAD_LEN:
            # Believing this length would stall framing until it was filled. Treat the header
            # as noise and resync, so a corrupt length field costs one byte, not seconds.
            resyncs += 1
            cursor = start + 1
            continue

        payload_start = length_end
        payload_end = payload_start + payload_len
        footer_end = payload_end + _FOOTER_LEN
        if footer_end > len(buffer):
            return FrameScan(payloads, buffer[start:], resyncs)

        if buffer[payload_end:footer_end] != DATA_FRAME_FOOTER:
            # A header-shaped byte sequence that was not really a header (noise, or a corrupt
            # length field). Resync by searching again one byte later rather than trusting the
            # length field a second time.
            resyncs += 1
            cursor = start + 1
            continue

        payloads.append(buffer[payload_start:payload_end])
        cursor = footer_end


def parse_targets(payload: bytes) -> list[RawTarget]:
    """Every target record in one data-frame payload.

    The wire's own `target_count` byte is read but not trusted over the payload's actual
    length: a would-be record that runs past the end of `payload` is dropped rather than
    parsed from truncated bytes, and a payload with no complete records at all is simply an
    empty result, not an error.
    """
    if len(payload) < _PAYLOAD_PREFIX_LEN:
        raise FrameError(f"payload too short to contain a target header: {len(payload)} bytes")

    targets: list[RawTarget] = []
    offset = _PAYLOAD_PREFIX_LEN
    while offset + _TARGET_RECORD_LEN <= len(payload):
        targets.append(_parse_target_record(payload[offset : offset + _TARGET_RECORD_LEN]))
        offset += _TARGET_RECORD_LEN
    return targets


def _parse_target_record(record: bytes) -> RawTarget:
    angle_byte, distance_byte, direction_byte, speed_byte, signal_byte = record
    return RawTarget(
        angle_deg=float(angle_byte - _ANGLE_ZERO_OFFSET),
        distance_m=float(distance_byte),
        direction_byte=direction_byte,
        speed_kph=float(speed_byte),
        signal_raw=signal_byte,
    )
