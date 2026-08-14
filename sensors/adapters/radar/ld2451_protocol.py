"""The radar's wire format: bytes in, target reports out. No I/O, no state beyond a buffer.

Split from the adapter so it can be built and tested *before the hardware exists*, and so the
part most likely to be wrong is the part most heavily exercised. Framing a byte stream is
where serial drivers actually fail — not in the arithmetic, but in resynchronisation after a
partial read, a dropped byte, or a payload that happens to contain the header sequence. All of
that is testable against synthesised bytes and none of it needs a radar.

--------------------------------------------------------------------------------------------
READ THIS BEFORE TRUSTING ANY FIELD VALUE
--------------------------------------------------------------------------------------------

`WIRE_FORMAT` below encodes the byte-level layout — header, footer, field order, units,
scaling — from the module's published serial protocol. **Every constant in it is unverified
against hardware.** They were written from documentation, not from a captured stream, and a
datasheet revision or a firmware difference will silently change any of them.

What that means in practice:

- The *framing logic* in this module is correct independently of those values. It is tested by
  synthesising frames from the same `WireFormat` it parses with, so the tests prove the
  algorithm — resync, partial frames, malformed payloads, header-in-payload — and say nothing
  about whether the constants match the device.
- The *constants* must be confirmed against a real capture before any detection this produces
  is believed. `docs/radar_bringup.md` is the procedure.
- Getting a constant wrong is not a loud failure. A wrong speed scale produces plausible
  targets moving at the wrong speed; a wrong direction convention produces tracks that close
  when they are opening. Neither raises.

Because a `WireFormat` is a value, correcting one is a data change rather than a code change:
capture a stream, compare, adjust the constants, and every test still applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ProtocolError(ValueError):
    """A frame could not be interpreted. Carried to the adapter, which counts rather than
    crashes: a corrupt frame on a serial line is an event to be measured, not an exception."""


@dataclass(frozen=True, slots=True)
class WireFormat:
    """The byte-level layout of the device's target-report frames.

    UNVERIFIED — see the module docstring. Held as a value so a correction is a data change.
    """

    header: bytes = b"\xf4\xf3\xf2\xf1"
    """Start-of-frame marker."""

    footer: bytes = b"\xf8\xf7\xf6\xf5"
    """End-of-frame marker."""

    length_bytes: int = 2
    """Width of the little-endian payload-length field that follows the header."""

    target_bytes: int = 5
    """Size of one target record within the payload."""

    header_target_count_bytes: int = 2
    """Payload preamble before the first target record: alarm state and target count."""

    angle_offset: int = 0x80
    """Value subtracted from the raw angle byte to give degrees relative to boresight.

    The device transmits an unsigned byte biased around this value so that a negative angle
    survives an unsigned channel. If this bias is wrong every bearing is wrong by a constant,
    which looks like a mis-aimed sensor rather than a decode error."""

    speed_scale_mps: float = 1.0 / 3.6
    """Multiplier converting the raw speed byte to metres per second. The device reports km/h."""

    approaching_flag: int = 0x01
    """Direction byte value meaning the target is closing on the sensor.

    The canonical schema signs `radial_velocity_mps` negative toward the sensor. Inverting this
    produces tracks that open when they are closing, which is plausible enough to go unnoticed
    and is the single most consequential constant here."""

    max_targets: int = 8
    """Most targets a single frame may report. A count above this means the frame is corrupt
    however well-formed it looked, and is rejected rather than trusted."""

    max_payload_bytes: int = 512
    """Ceiling on the declared payload length. A corrupt length field can claim a huge payload;
    without a bound the reader would wait forever for bytes that are not coming, and a stalled
    reader is indistinguishable from a quiet sky (FM-010)."""

    def representable_range_m(self) -> tuple[float, float]:
        """Distances a single-byte field can express.

        The wire format bounds the sensor's usable envelope regardless of what the datasheet
        claims, and `radar.coverage` in configuration must sit inside it. Exposed so that
        constraint can be asserted rather than discovered when a distant target wraps.
        """
        return (0.0, 255.0)

    def representable_angle_deg(self) -> tuple[float, float]:
        """Angles the biased single-byte field can express, relative to boresight."""
        return (float(-self.angle_offset), float(255 - self.angle_offset))

    def representable_speed_mps(self) -> tuple[float, float]:
        """Speeds the single-byte field can express at the configured scale."""
        return (0.0, 255 * self.speed_scale_mps)


WIRE_FORMAT = WireFormat()
"""The default layout. Replace the constants here after verifying against a capture."""


@dataclass(frozen=True, slots=True)
class RawTarget:
    """One target exactly as the device reported it, in the device's own units and frame.

    Deliberately not a `Detection`. Converting to the canonical schema needs the sensor's
    mounting geometry and the site frame, which this module does not have and must not acquire
    — keeping the decode pure is what lets it be tested against bytes alone.
    """

    angle_deg: float
    """Azimuth relative to the sensor's boresight. Positive clockwise."""

    distance_m: float

    radial_velocity_mps: float
    """Signed: negative toward the sensor, positive away. Canonical convention, applied here so
    that the device's separate direction flag cannot leak downstream."""

    signal_strength: int
    """Device-native figure. Not a signal-to-noise ratio in decibels, and must not be reported
    as one — the adapter maps it to a confidence and leaves `snr_db` None."""


@dataclass(slots=True)
class FrameStats:
    """What the reader saw. The adapter's health report is built from these.

    Mutable by design: it is a running tally owned by one decoder and incremented as bytes
    arrive. Everything else in this module is frozen, because everything else is a value.

    Counted rather than logged because the interesting signal is a *rate*: an occasional
    rejected frame is a normal serial line, a rising proportion is a cable or grounding fault,
    and only a count over time can tell those apart.

    There is deliberately no `checksum_failures` here. This decoder validates framing — header,
    declared length, footer — and computes no checksum, because whether the device appends one
    and over which bytes is unverified. A counter that was declared and never incremented would
    make `health()` look like it was watching for corruption while watching for nothing.
    Confirming the checksum is a bring-up step; see `docs/radar_bringup.md`.
    """

    frames_decoded: int = 0
    malformed_frames: int = 0
    resync_events: int = 0
    bytes_discarded: int = 0


@dataclass
class FrameDecoder:
    """Accumulates bytes and yields complete, validated frames.

    Stateful only in its buffer. Feed it whatever a read returned — one byte, a full frame, or
    three and a half frames — and it yields the target reports that are complete, keeping the
    remainder for next time.

    ## Resynchronisation

    The reader must recover from garbage rather than desynchronise permanently. Three cases,
    all of which happen on a real line:

    - **A partial frame at startup.** The first read almost never begins on a frame boundary,
      so leading bytes that are not a header are discarded until one is found.
    - **A dropped or corrupted byte mid-frame.** The footer will not be where the length said,
      so the frame is rejected and the scan resumes *after the failed header*, not at the
      buffer start — resuming at the start would find the same bad header forever.
    - **The header sequence appearing inside a payload.** Legitimate, since payload bytes are
      arbitrary. Handled by validating length and footer together: a false header fails that
      check and the scan moves on.
    """

    wire: WireFormat = field(default_factory=lambda: WIRE_FORMAT)
    stats: FrameStats = field(default_factory=FrameStats)
    _buffer: bytearray = field(default_factory=bytearray, repr=False)

    def feed(self, chunk: bytes) -> list[list[RawTarget]]:
        """Add bytes and return the target lists of every frame that completed.

        A frame reporting zero targets yields an empty list — which is a real observation
        meaning "nothing in view", not an absence of data, and is therefore preserved rather
        than dropped.
        """
        self._buffer.extend(chunk)

        completed: list[list[RawTarget]] = []
        while True:
            frame = self._take_frame()
            if frame is None:
                break
            targets = self._decode(frame)
            if targets is not None:
                self.stats.frames_decoded += 1
                completed.append(targets)

        # Bounded *after* draining, never before. Trimming on the way in discards whatever is
        # at the front of the buffer, and at the front of the buffer is the oldest complete
        # frame — so a read larger than the ceiling destroyed valid data before anything looked
        # at it. At 115200 baud a 0.2s read returns ~2300 bytes against a 2048-byte ceiling, so
        # that was not an edge case; it fired on every full read. What remains here is by
        # definition an incomplete frame, which is the only thing safe to drop.
        self._bound_buffer()
        return completed

    def _bound_buffer(self) -> None:
        """Keep the buffer from growing without limit when no valid frame ever arrives.

        A device sending continuous garbage, or a mis-set baud rate, produces bytes that never
        contain a header. Without a bound the buffer grows until the process dies, which on an
        unattended system is a far worse failure than dropping bytes.

        Called only after `feed` has extracted every complete frame, so the residue is a
        partial frame at most. Calling it earlier would discard complete frames instead.
        """
        ceiling = self.wire.max_payload_bytes * 4
        if len(self._buffer) > ceiling:
            excess = len(self._buffer) - ceiling
            del self._buffer[:excess]
            self.stats.bytes_discarded += excess

    def _take_frame(self) -> bytes | None:
        """Remove and return the next complete, footer-validated frame payload, if any."""
        wire = self.wire

        while True:
            start = self._buffer.find(wire.header)
            if start < 0:
                # No header in the buffer. Keep only a trailing fragment that could still be
                # the beginning of one, so a header split across two reads is not lost.
                keep = len(wire.header) - 1
                if len(self._buffer) > keep:
                    discarded = len(self._buffer) - keep
                    del self._buffer[:discarded]
                    self.stats.bytes_discarded += discarded
                return None

            if start > 0:
                self.stats.bytes_discarded += start
                self.stats.resync_events += 1
                del self._buffer[:start]

            length_at = len(wire.header)
            if len(self._buffer) < length_at + wire.length_bytes:
                return None  # Length field has not arrived yet.

            declared = int.from_bytes(
                self._buffer[length_at : length_at + wire.length_bytes], "little"
            )
            if declared > wire.max_payload_bytes:
                self._discard_false_header()
                continue

            payload_at = length_at + wire.length_bytes
            end = payload_at + declared
            if len(self._buffer) < end + len(wire.footer):
                return None  # Payload or footer still in flight.

            if bytes(self._buffer[end : end + len(wire.footer)]) != wire.footer:
                # Either a corrupted frame or a header sequence occurring inside a payload.
                # Both are handled the same way: this was not a frame, resume past it.
                self._discard_false_header()
                continue

            payload = bytes(self._buffer[payload_at:end])
            del self._buffer[: end + len(wire.footer)]
            return payload

    def _discard_false_header(self) -> None:
        """Drop the leading header so the next scan cannot rediscover it.

        Advancing by one byte rather than by a whole header lets a genuine header that overlaps
        the false one still be found.
        """
        del self._buffer[:1]
        self.stats.bytes_discarded += 1
        self.stats.resync_events += 1

    def _decode(self, payload: bytes) -> list[RawTarget] | None:
        """Turn a validated payload into target reports, or None if it does not make sense."""
        wire = self.wire

        if len(payload) < wire.header_target_count_bytes:
            self.stats.malformed_frames += 1
            return None

        count = payload[wire.header_target_count_bytes - 1]
        body = payload[wire.header_target_count_bytes :]

        if count > wire.max_targets or len(body) < count * wire.target_bytes:
            # A count the frame cannot substantiate. Rejected whole: decoding the targets that
            # do fit would report a subset of a frame we have already established is wrong.
            self.stats.malformed_frames += 1
            return None

        return [
            self._target(body[index * wire.target_bytes : (index + 1) * wire.target_bytes])
            for index in range(count)
        ]

    def _target(self, record: bytes) -> RawTarget:
        wire = self.wire
        angle_raw, distance_raw, direction_raw, speed_raw, strength = record[:5]

        speed = speed_raw * wire.speed_scale_mps
        # Canonical sign convention: negative closes on the sensor. Applied at the boundary so
        # the device's separate direction flag never travels further into the system.
        signed = -speed if direction_raw == wire.approaching_flag else speed

        return RawTarget(
            angle_deg=float(angle_raw - wire.angle_offset),
            distance_m=float(distance_raw),
            radial_velocity_mps=signed,
            signal_strength=int(strength),
        )


def build_frame(targets: list[RawTarget], *, wire: WireFormat = WIRE_FORMAT) -> bytes:
    """Encode target reports as the device would transmit them.

    The inverse of the decoder, and the reason the decoder can be tested at all: it lets a test
    state its intent in target terms and check that the same targets survive the round trip.
    Test-facing but production code, because a test helper that drifts from the decoder tests
    nothing.
    """
    body = bytearray()
    for target in targets:
        speed_raw = round(abs(target.radial_velocity_mps) / wire.speed_scale_mps)
        direction = wire.approaching_flag if target.radial_velocity_mps < 0 else 0x00
        # round(), not int(): truncation goes toward zero, so it biases in opposite directions
        # either side of boresight and makes the round trip asymmetric about the axis.
        angle_raw = round(target.angle_deg) + wire.angle_offset

        # Named rather than left to `bytes()` raising "byte must be in range(0, 256)", which
        # says nothing about which field overflowed or what the limit means. The limits are a
        # property of the wire format, so a target outside them is one this device could not
        # have reported — worth stating plainly wherever it is encountered.
        for name, raw, limit in (
            ("angle_deg", angle_raw, wire.representable_angle_deg()),
            ("distance_m", int(target.distance_m), wire.representable_range_m()),
            ("radial_velocity_mps", speed_raw, wire.representable_speed_mps()),
            ("signal_strength", target.signal_strength, (0, 255)),
        ):
            if not 0 <= raw <= 255:
                raise ProtocolError(
                    f"{name}={getattr(target, name)} is outside what this wire format can "
                    f"express ({limit[0]} to {limit[1]}); the device could not report it"
                )

        body.extend(
            (
                angle_raw,
                round(target.distance_m),
                direction,
                speed_raw,
                target.signal_strength,
            )
        )

    payload = bytearray(wire.header_target_count_bytes)
    payload[wire.header_target_count_bytes - 1] = len(targets)
    payload.extend(body)

    return b"".join(
        (
            wire.header,
            len(payload).to_bytes(wire.length_bytes, "little"),
            bytes(payload),
            wire.footer,
        )
    )
