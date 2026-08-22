"""The LD2451 wire format: bytes in, target reports out, with integrity accounting.

**Pure codec. No I/O of any kind.** Nothing here opens a port, sleeps, or reads a clock. It is a
state machine over a byte stream, which is what makes it testable exhaustively — against
truncation, desynchronisation, corruption and garbage — with no hardware present. A parser that
can only be tested against a live device is a parser that cannot be tested.

## The frame

Little-endian throughout.

    F4 F3 F2 F1 | length (uint16) | count | alarm | target * N | F8 F7 F6 F5
                                                    └── 5 bytes each ──┘

    target:  angle | distance | direction | speed | snr
             │       │          │           │       └─ 0..100
             │       │          │           └───────── whole km/h
             │       │          └───────────────────── 01 approaching, 00 receding
             │       └──────────────────────────────── whole metres
             └──────────────────────────────────────── degrees, offset by 0x80

`length` counts the payload only: `count`, `alarm` and the targets, so `length == 5N + 2`.

## There is no checksum

Not a weak one — none. Header, length, payload, tail, and nothing else. This is the defining
property of the format and it shapes everything below. ADR 0018 has the full reasoning; the
short version:

- Corruption that changes length or alignment **is** caught, by the length-versus-tail
  agreement. The frame is discarded and the decoder resynchronises. Benign: the sensor appears
  to have missed one update, which the tracker already handles.
- Corruption that changes a payload byte in place is **not** caught by the framing at all. A
  single bit flip turns a target at 40 m (`0x28`) into one at 168 m (`0xA8`) in a frame that is
  structurally perfect.

So the only integrity check available for values is the **unused code space in each field**, and
that is what `PlausibilityEnvelope` is. It is not a heuristic filter bolted on afterwards; on
this protocol it *is* the integrity mechanism, which is why the probability of it letting
corruption through is computed rather than assumed — see `residual_corruption_probability`.

Two consequences worth knowing before configuring one:

1. **A tighter envelope is a measurably better link.** Narrowing the speed ceiling from 200 km/h
   to a figure the site actually expects moves the residual materially. Declaring real bounds is
   not merely tidy.
2. **Every rejection is counted, by reason.** Undetected corruption cannot be observed directly,
   but it shares a physical cause with the corruption that *is* detected, so the observable
   reject rate is an estimator for the unobservable one. A decoder that discarded malformed
   frames quietly would report a healthy sensor on a failing cable.

## What this deliberately does not do

**No temporal filtering.** A target that appears far from where anything was on the previous
frame is not rejected here. A real object genuinely can appear anywhere on the first frame it is
seen, and deciding whether a sequence of observations is one object is association — the
tracker's job, done with covariance information this module does not have.

**No N-of-M confirmation.** `TrackManager` already requires corroboration before a track is
confirmed, sized against the association gate. Doing it again here would silently raise the
effective detection threshold and hide the change from the conformance suite.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

DATA_FRAME_HEADER = b"\xf4\xf3\xf2\xf1"
DATA_FRAME_TAIL = b"\xf8\xf7\xf6\xf5"

COMMAND_FRAME_HEADER = b"\xfd\xfc\xfb\xfa"
COMMAND_FRAME_TAIL = b"\x04\x03\x02\x01"
"""Configuration acknowledgements share the UART with target reports.

Recognised and skipped rather than treated as corruption. Without this, every ACK the module
sends after a configuration command would be counted as a desynchronisation, inflating exactly
the rejection rate that is supposed to mean the cable is failing.

The tail is the family convention and is **not confirmed against the physical unit**. If it
differs, command frames fall through to resynchronisation — visibly, in the counters, rather
than silently — and correcting it is a one-constant change.
"""

DELIMITER_LENGTH = 4
LENGTH_FIELD_LENGTH = 2
PAYLOAD_PREFIX_LENGTH = 2
"""`count` and `alarm`, which precede the targets."""

TARGET_LENGTH = 5
ANGLE_OFFSET = 0x80
"""Reported angle is `byte - 0x80`, so 0x80 is boresight."""

BYTE_VALUES = 256
CORRUPTED_ALTERNATIVES = BYTE_VALUES - 1
"""A corrupted byte takes one of the 255 values that are not the original one. Using 256 here
would understate the residual by assuming corruption can leave a byte unchanged."""


class RejectReason(StrEnum):
    """Why a candidate frame was discarded.

    Counted separately rather than totalled, because they mean different things about the link:
    a desynchronisation is a byte-level fault, a bad tail is a length or alignment fault, and an
    implausible value is a payload fault in an otherwise well-formed frame — which is the
    closest observable proxy for the corruption this protocol cannot detect at all.
    """

    RESYNCHRONISED = "resynchronised"
    """Bytes discarded while searching for a header. The signature of a byte-level fault, and
    the first thing to rise on a failing cable or a wrong baud rate."""

    IMPLAUSIBLE_LENGTH = "implausible_length"
    """The length field is not `5N + 2` for any admissible target count."""

    TAIL_MISMATCH = "tail_mismatch"
    """Header and length were consistent but the tail was not where they said it would be."""

    IMPLAUSIBLE_VALUE = "implausible_value"
    """A well-framed frame carrying a value the sensor cannot physically have produced."""


class PlausibilityEnvelope(BaseModel):
    """The bounds a target report must satisfy to be believable.

    On a protocol with a checksum this would be defensive tidiness. Here it is the integrity
    check, and its tightness is the operator's only lever on how much corruption gets through —
    so every bound is configuration, and none of it is a constant in this module.

    Bounds describe **what this sensor can physically produce at this site**, not what is
    interesting. A drone slower than `max_speed_kmh` is still reported; the ceiling exists to
    reject a byte value the hardware could not have generated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_azimuth_deg: int = Field(ge=1, le=127)
    """Half-aperture. The device reports within a fixed cone fixed relative to its board, so a
    reading well outside it is corruption rather than a wide-angle detection. Set it a little
    above the rated aperture: the rating is a beamwidth, not a hard cutoff."""

    min_distance_m: int = Field(ge=0, le=255)
    max_distance_m: int = Field(ge=1, le=255)
    """Distance is a single byte of whole metres, so 255 m is the format's ceiling regardless of
    what the device is configured to detect."""

    max_speed_kmh: int = Field(ge=1, le=255)
    """The single most valuable bound to tighten. Speed has the widest legal code space of any
    target field, so it lets through more corruption than the rest combined."""

    min_snr: int = Field(default=0, ge=0, le=255)
    max_snr: int = Field(default=100, ge=0, le=255)
    """The device's documented range is 0–100, so values above it are corruption by
    construction."""

    max_targets: int = Field(ge=1, le=50)
    """Caps the frame size the decoder will accept, which is what stops a corrupted length field
    from making it wait for a frame that will never arrive."""

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> PlausibilityEnvelope:
        if self.min_distance_m >= self.max_distance_m:
            raise ValueError("min_distance_m must be below max_distance_m")
        if self.min_snr >= self.max_snr:
            raise ValueError("min_snr must be below max_snr")
        return self

    @property
    def max_payload_length(self) -> int:
        return TARGET_LENGTH * self.max_targets + PAYLOAD_PREFIX_LENGTH

    @property
    def max_frame_length(self) -> int:
        return (
            DELIMITER_LENGTH
            + LENGTH_FIELD_LENGTH
            + self.max_payload_length
            + DELIMITER_LENGTH
        )

    def legal_angle_values(self) -> int:
        low = max(ANGLE_OFFSET - self.max_azimuth_deg, 0)
        high = min(ANGLE_OFFSET + self.max_azimuth_deg, BYTE_VALUES - 1)
        return high - low + 1

    def legal_distance_values(self) -> int:
        return self.max_distance_m - self.min_distance_m + 1

    def legal_speed_values(self) -> int:
        return self.max_speed_kmh + 1

    def legal_snr_values(self) -> int:
        return self.max_snr - self.min_snr + 1


@dataclass(frozen=True, slots=True)
class TargetReport:
    """One target as the sensor described it, in the sensor's own terms.

    Deliberately still in the device's units and frame — degrees off boresight, whole metres,
    km/h. Converting to WGS-84 and SI is the adapter's job, not the codec's, and keeping the two
    apart is what lets this module be tested against captured bytes with no geometry involved.
    """

    angle_deg: int
    """Degrees from boresight. Positive and negative senses are as the device reports them; the
    adapter resolves that against the placement's boresight."""

    distance_m: int
    approaching: bool
    speed_kmh: int
    snr: int
    """Signal-to-noise, 0–100. The device's own confidence that this is a real return, and the
    natural input to the canonical detection's `quality.confidence`."""


@dataclass(frozen=True, slots=True)
class TargetFrame:
    """One complete, structurally valid, plausible report frame."""

    alarm: bool
    """The device's own summary flag: something is approaching. Carried because it is part of
    the frame, and cross-checked against the target directions as a further integrity signal."""

    targets: tuple[TargetReport, ...]


@dataclass(slots=True)
class DecoderStatistics:
    """What the link has done, in counts.

    The observable half of ADR 0018. Undetected payload corruption cannot be measured, but it
    shares a physical cause with the corruption that is detected — a failing cable, a wrong baud
    rate, interference, a buffer overrun — so these counts are the evidence on which a source is
    degraded.
    """

    frames_decoded: int = 0
    targets_decoded: int = 0
    command_frames_skipped: int = 0
    bytes_consumed: int = 0
    bytes_discarded: int = 0

    alarm_direction_disagreements: int = 0
    """Frames whose alarm flag contradicted every target's direction.

    An integrity signal rather than a rejection reason, because the flag's exact semantics are
    unconfirmed against hardware and dropping frames over it could discard real detections. A
    count that stays at zero on a live unit would confirm the reading; one that tracks the
    reject rate points at the cable; one that is consistently high on a clean link means the
    flag means something other than what the document says. See `_decode_payload`.
    """

    rejects: Counter[RejectReason] = field(default_factory=Counter)

    @property
    def frames_rejected(self) -> int:
        """Candidate frames discarded. Excludes resynchronisation, which counts bytes rather
        than frames and would otherwise dominate the ratio for no meaningful reason."""
        return sum(
            count
            for reason, count in self.rejects.items()
            if reason is not RejectReason.RESYNCHRONISED
        )

    @property
    def frame_reject_rate(self) -> float:
        """Rejected candidates as a fraction of all candidates seen. Zero when nothing has
        arrived, which is not the same as a healthy link and is why the adapter reports the
        counts as well as the rate."""
        candidates = self.frames_decoded + self.frames_rejected
        return self.frames_rejected / candidates if candidates else 0.0

    @property
    def byte_discard_rate(self) -> float:
        """Bytes thrown away while resynchronising, as a fraction of all bytes seen.

        The most sensitive available indicator. Framing errors shift the whole stream, so a
        marginal line shows up here long before it produces a frame that fails a value check.
        """
        total = self.bytes_consumed + self.bytes_discarded
        return self.bytes_discarded / total if total else 0.0

    def estimated_undetected_rate(self, residual: float) -> float:
        """Frames per decoded frame that are probably corrupt and were accepted anyway.

        The step ADR 0018 turns the accounting into: with a known probability `residual` that
        corruption survives the checks, an observed rate of *detected* corruption implies an
        unobserved rate of the rest.

            undetected  ≈  detected × residual / (1 − residual)

        An estimate from a model, not a measurement, and it inherits the single-byte corruption
        assumption behind `residual` — which is conservative for the framing errors that
        actually dominate on a UART. It is reported so that "the link is degrading" can be said
        with a number attached rather than as an impression.
        """
        if residual <= 0.0:
            return 0.0
        if residual >= 1.0:
            return float("inf")
        return self.frame_reject_rate * residual / (1.0 - residual)


class ProtocolError(ValueError):
    """A frame could not be decoded. Carries the reason so the caller can count it."""

    def __init__(self, reason: RejectReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def residual_corruption_probability(
    envelope: PlausibilityEnvelope, targets: int | None = None
) -> float:
    """Probability that a single corrupted byte yields a frame the decoder accepts.

    Computed rather than estimated, because on a checksumless protocol this number *is* the
    integrity guarantee and a hand-waved one would be worthless. For each byte position, the
    chance corruption survives is the fraction of the 255 alternative values that would still
    pass every check:

        p_undetected(i)  =  (legal_values(i) − 1) / 255
        residual         =  mean over all byte positions

    Header, length, count and tail bytes each have exactly one legal value — the length must
    agree with the count and the tail's position, so corrupting either is always detected — and
    contribute zero. Everything that gets through, gets through the target fields.

    `targets` defaults to the envelope's maximum, which is the **worst case**: the fully-checked
    delimiter bytes are a fixed cost per frame, so the more targets a frame carries, the smaller
    their protective share of it. Declaring the worst case is the right direction for a
    safety-relevant figure to err in. Pass a specific count to see a typical frame instead.
    """
    count = envelope.max_targets if targets is None else targets
    if count < 0:
        raise ValueError(f"targets must not be negative, got {count}")

    # Header (4), length (2), count (1) and tail (4) each admit exactly one value: the length
    # field must equal 5N+2 and the tail must sit where that says, so any change to any of them
    # breaks an equation the decoder checks. They contribute nothing to the residual.
    fully_checked_bytes = DELIMITER_LENGTH + LENGTH_FIELD_LENGTH + 1 + DELIMITER_LENGTH

    # The alarm byte has two legal values, so it is very nearly a parity check on itself.
    undetected = _survival(2)

    per_target = (
        _survival(envelope.legal_angle_values())
        + _survival(envelope.legal_distance_values())
        + _survival(2)  # direction: 01 approaching, 00 receding
        + _survival(envelope.legal_speed_values())
        + _survival(envelope.legal_snr_values())
    )
    undetected += per_target * count

    total_bytes = fully_checked_bytes + 1 + TARGET_LENGTH * count
    return undetected / total_bytes


def _survival(legal_values: int) -> float:
    """Chance that corrupting a byte with `legal_values` admissible settings goes unnoticed."""
    return max(legal_values - 1, 0) / CORRUPTED_ALTERNATIVES


class FrameDecoder:
    """Byte stream in, complete frames out, with everything discarded accounted for.

    Stateful because it must be: frames arrive split across reads, and resynchronisation after a
    fault is a property of the stream rather than of any one frame. `feed` is the whole
    interface; call it with whatever arrived and take the frames it returns.
    """

    def __init__(self, envelope: PlausibilityEnvelope) -> None:
        self._envelope = envelope
        self._buffer = bytearray()
        self.statistics = DecoderStatistics()

    @property
    def envelope(self) -> PlausibilityEnvelope:
        return self._envelope

    @property
    def residual_corruption_probability(self) -> float:
        """Worst-case probability that corruption survives this decoder's checks."""
        return residual_corruption_probability(self._envelope)

    @property
    def pending_bytes(self) -> int:
        """Bytes held awaiting the rest of a frame. Bounded by one maximum frame."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[TargetFrame]:
        """Consume bytes and return every complete frame they completed.

        Returns an empty list when nothing completed, which is the normal case for a partial
        read and means exactly that — never that something went wrong. Faults are counted in
        `statistics`, not raised, because one bad frame must not stop the stream.
        """
        self._buffer.extend(data)
        frames: list[TargetFrame] = []

        while True:
            frame = self._take_one()
            if frame is _INCOMPLETE:
                break
            if frame is not None:
                frames.append(frame)

        return frames

    def _take_one(self) -> TargetFrame | None | _Incomplete:
        """Advance past exactly one frame, one skipped command frame, or one fault.

        Returns the frame, None for progress that produced no frame, or `_INCOMPLETE` when more
        bytes are needed. Structured this way so `feed` cannot loop forever: every path except
        `_INCOMPLETE` removes at least one byte from the buffer.
        """
        start = self._find_header()
        if start is _INCOMPLETE:
            return _INCOMPLETE

        if self._buffer[start : start + DELIMITER_LENGTH] == COMMAND_FRAME_HEADER:
            return self._skip_command_frame(start)

        header_end = start + DELIMITER_LENGTH
        if len(self._buffer) < header_end + LENGTH_FIELD_LENGTH:
            return _INCOMPLETE

        length = int.from_bytes(
            self._buffer[header_end : header_end + LENGTH_FIELD_LENGTH], "little"
        )

        if not self._length_is_admissible(length):
            self._reject(
                RejectReason.IMPLAUSIBLE_LENGTH,
                # One byte, not the whole claimed frame: the length is exactly the field we have
                # established cannot be trusted, so using it to decide how far to skip would
                # compound the error.
                discard=1,
            )
            return None

        payload_start = header_end + LENGTH_FIELD_LENGTH
        payload_end = payload_start + length
        frame_end = payload_end + DELIMITER_LENGTH

        if len(self._buffer) < frame_end:
            return _INCOMPLETE

        if bytes(self._buffer[payload_end:frame_end]) != DATA_FRAME_TAIL:
            self._reject(RejectReason.TAIL_MISMATCH, discard=1)
            return None

        try:
            frame = self._decode_payload(bytes(self._buffer[payload_start:payload_end]))
        except ProtocolError as error:
            # The framing held — four exact tail bytes at a computed offset is not a coincidence
            # worth entertaining — so the frame's extent is known and trustworthy even though
            # its contents are not. Skipping the whole thing is right; rescanning inside it
            # would only find noise.
            self._reject(error.reason, discard=frame_end)
            return None

        self.statistics.frames_decoded += 1
        self.statistics.targets_decoded += len(frame.targets)
        self.statistics.bytes_consumed += frame_end
        del self._buffer[:frame_end]
        return frame

    def _find_header(self) -> int | _Incomplete:
        """Index of the next frame header, discarding whatever precedes it.

        Bytes before a header did not belong to any frame this decoder could read, so they are
        counted as discarded rather than dropped quietly — that count is the earliest signal a
        link has begun to fail.
        """
        data = bytes(self._buffer)
        candidates = [
            index
            for index in (data.find(DATA_FRAME_HEADER), data.find(COMMAND_FRAME_HEADER))
            if index >= 0
        ]

        if not candidates:
            # Keep only what could still be the start of a header split across two reads.
            keep = DELIMITER_LENGTH - 1
            if len(self._buffer) > keep:
                self._discard(len(self._buffer) - keep)
            return _INCOMPLETE

        start = min(candidates)
        if start > 0:
            self._discard(start)
        return 0

    def _skip_command_frame(self, start: int) -> None | _Incomplete:
        """Step over a configuration acknowledgement sharing the UART.

        Not corruption, and counting it as such would make the reject rate meaningless every
        time the module was configured.
        """
        header_end = start + DELIMITER_LENGTH
        if len(self._buffer) < header_end + LENGTH_FIELD_LENGTH:
            return _INCOMPLETE

        length = int.from_bytes(
            self._buffer[header_end : header_end + LENGTH_FIELD_LENGTH], "little"
        )
        if length > self._envelope.max_payload_length + DELIMITER_LENGTH:
            self._reject(RejectReason.IMPLAUSIBLE_LENGTH, discard=1)
            return None

        frame_end = header_end + LENGTH_FIELD_LENGTH + length + DELIMITER_LENGTH
        if len(self._buffer) < frame_end:
            return _INCOMPLETE

        if bytes(self._buffer[frame_end - DELIMITER_LENGTH : frame_end]) != COMMAND_FRAME_TAIL:
            # The command tail is the family convention and is unconfirmed against the physical
            # unit. A mismatch resynchronises visibly instead of consuming bytes on a guess.
            self._reject(RejectReason.TAIL_MISMATCH, discard=1)
            return None

        self.statistics.command_frames_skipped += 1
        self.statistics.bytes_consumed += frame_end
        del self._buffer[:frame_end]
        return None

    def _length_is_admissible(self, length: int) -> bool:
        """Whether a length field could describe a real frame.

        `length == 5N + 2` exactly, for an N the envelope permits. This single equation is what
        catches every length-changing corruption, and it is also what stops a corrupted length
        from making the decoder wait for a frame that will never arrive.
        """
        if length < PAYLOAD_PREFIX_LENGTH or length > self._envelope.max_payload_length:
            return False
        return (length - PAYLOAD_PREFIX_LENGTH) % TARGET_LENGTH == 0

    def _decode_payload(self, payload: bytes) -> TargetFrame:
        """Turn a well-framed payload into targets, refusing values the device cannot produce."""
        count = payload[0]
        alarm_byte = payload[1]

        declared = (len(payload) - PAYLOAD_PREFIX_LENGTH) // TARGET_LENGTH
        if count != declared:
            raise ProtocolError(
                RejectReason.IMPLAUSIBLE_VALUE,
                f"target count {count} disagrees with the {declared} the length field implies",
            )
        if count > self._envelope.max_targets:
            raise ProtocolError(
                RejectReason.IMPLAUSIBLE_VALUE,
                f"target count {count} exceeds the configured maximum of "
                f"{self._envelope.max_targets}",
            )
        if alarm_byte > 1:
            raise ProtocolError(
                RejectReason.IMPLAUSIBLE_VALUE,
                f"alarm byte is 0x{alarm_byte:02X}; only 0x00 and 0x01 are defined",
            )

        targets = tuple(
            self._decode_target(
                payload[
                    PAYLOAD_PREFIX_LENGTH
                    + index * TARGET_LENGTH : PAYLOAD_PREFIX_LENGTH
                    + (index + 1) * TARGET_LENGTH
                ]
            )
            for index in range(count)
        )

        alarm = alarm_byte == 1
        if alarm and targets and not any(report.approaching for report in targets):
            # Counted, not rejected, and the distinction is deliberate.
            #
            # The summary flag and the per-target directions look like two encodings of one
            # fact, which would make disagreement a free corruption check. But the flag's exact
            # semantics are documented only as "there is a target approaching" and are *not*
            # confirmed against a physical unit — if it in fact means "a target is present", or
            # latches across frames, then rejecting on disagreement would silently discard
            # frames full of individually plausible targets.
            #
            # Losing real detections is the one failure this system must not have, and it is
            # strictly worse than admitting a corrupted flag nothing downstream reads. So the
            # disagreement becomes an integrity signal in the statistics — where a rising count
            # is visible and can be investigated against the hardware — rather than a rejection.
            # If the semantics are ever confirmed, promoting this to a rejection is a two-line
            # change and a better-founded one.
            self.statistics.alarm_direction_disagreements += 1

        return TargetFrame(alarm=alarm, targets=targets)

    def _decode_target(self, raw: bytes) -> TargetReport:
        angle_byte, distance, direction, speed, snr = raw
        envelope = self._envelope

        angle = angle_byte - ANGLE_OFFSET
        if abs(angle) > envelope.max_azimuth_deg:
            raise ProtocolError(
                RejectReason.IMPLAUSIBLE_VALUE,
                f"angle {angle}° is outside the configured +/-{envelope.max_azimuth_deg}° "
                "aperture; this device reports within a cone fixed relative to its board",
            )
        if not envelope.min_distance_m <= distance <= envelope.max_distance_m:
            raise ProtocolError(
                RejectReason.IMPLAUSIBLE_VALUE,
                f"distance {distance} m is outside the configured "
                f"{envelope.min_distance_m}–{envelope.max_distance_m} m range",
            )
        if direction > 1:
            raise ProtocolError(
                RejectReason.IMPLAUSIBLE_VALUE,
                f"direction byte is 0x{direction:02X}; only 0x00 and 0x01 are defined",
            )
        if speed > envelope.max_speed_kmh:
            raise ProtocolError(
                RejectReason.IMPLAUSIBLE_VALUE,
                f"speed {speed} km/h exceeds the configured {envelope.max_speed_kmh} km/h "
                "ceiling",
            )
        if not envelope.min_snr <= snr <= envelope.max_snr:
            raise ProtocolError(
                RejectReason.IMPLAUSIBLE_VALUE,
                f"signal-to-noise {snr} is outside the documented "
                f"{envelope.min_snr}–{envelope.max_snr} range",
            )

        return TargetReport(
            angle_deg=angle,
            distance_m=distance,
            approaching=direction == 1,
            speed_kmh=speed,
            snr=snr,
        )

    def _reject(self, reason: RejectReason, *, discard: int) -> None:
        self.statistics.rejects[reason] += 1
        self._discard(discard, as_resynchronisation=False)

    def _discard(self, count: int, *, as_resynchronisation: bool = True) -> None:
        if count <= 0:
            return
        self.statistics.bytes_discarded += count
        if as_resynchronisation:
            self.statistics.rejects[RejectReason.RESYNCHRONISED] += count
        del self._buffer[:count]


class _Incomplete:
    """Sentinel: more bytes are needed. A distinct type so it can never be mistaken for a
    frame or for None, both of which `_take_one` also returns and which mean other things."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover — diagnostic only
        return "<incomplete>"


_INCOMPLETE = _Incomplete()


def encode_target_frame(frame: TargetFrame) -> bytes:
    """Build the bytes a device would have sent for this frame.

    Exists for the tests, and is the reason they can be exhaustive: a decoder tested only
    against captured samples is tested against the cases somebody happened to capture. With an
    encoder, every frame the format admits can be round-tripped, and corruption can be injected
    at a chosen byte to check that it is caught.

    Not used at runtime — nothing in Aetos transmits target reports, and nothing should.
    """
    payload = bytearray([len(frame.targets), 1 if frame.alarm else 0])
    for target in frame.targets:
        payload.extend(
            (
                target.angle_deg + ANGLE_OFFSET,
                target.distance_m,
                1 if target.approaching else 0,
                target.speed_kmh,
                target.snr,
            )
        )

    return b"".join(
        (
            DATA_FRAME_HEADER,
            len(payload).to_bytes(LENGTH_FIELD_LENGTH, "little"),
            bytes(payload),
            DATA_FRAME_TAIL,
        )
    )


def assumed_altitude_sigma_m(range_m: float, vertical_aperture_deg: float) -> float:
    """One-sigma vertical uncertainty for a sensor with no elevation channel.

    The device measures range and azimuth only, so an adapter must assume an altitude and say
    how little that assumption is worth (ADR 0019). Within the vertical beam it has no
    information favouring any elevation over any other, so the honest prior is uniform over the
    beam's extent at that range, whose standard deviation is half-width over root three.

    Lives here rather than in the adapter because the aperture is a property of this device and
    the formula is the reason the number is what it is; both belong with the hardware.
    """
    if range_m < 0.0:
        raise ValueError(f"range_m must not be negative, got {range_m}")
    half_extent_m = range_m * math.tan(math.radians(vertical_aperture_deg) / 2.0)
    return half_extent_m / math.sqrt(3.0)
