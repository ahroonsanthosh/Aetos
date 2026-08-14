"""Framing and decode, driven entirely by synthesised bytes. No hardware, no serial port.

What these prove and what they do not:

- They prove the **algorithm**: that a byte stream is resynchronised after garbage, that
  partial frames are held rather than mis-decoded, that a header appearing inside a payload
  does not derail the reader, that a corrupt length cannot stall it.
- They prove **nothing about the constants**. Frames are built with the same `WireFormat` they
  are parsed with, so a header byte that disagrees with the real device would pass every test
  here. Verifying the constants needs a capture from the hardware, which is what
  `docs/radar_bringup.md` is for.

That split is deliberate. The framing logic is where serial drivers actually break and it can
be finished now; the constants cannot be settled without the device, and pretending otherwise
would be worse than saying so.
"""

from __future__ import annotations

import pytest

from sensors.adapters.radar.ld2451_protocol import (
    WIRE_FORMAT,
    FrameDecoder,
    ProtocolError,
    RawTarget,
    WireFormat,
    build_frame,
)


def _target(
    *,
    angle_deg: float = 10.0,
    distance_m: float = 40.0,
    radial_velocity_mps: float = -5.0,
    signal_strength: int = 120,
) -> RawTarget:
    return RawTarget(
        angle_deg=angle_deg,
        distance_m=distance_m,
        radial_velocity_mps=radial_velocity_mps,
        signal_strength=signal_strength,
    )


class TestCleanStream:
    def test_a_single_frame_round_trips(self) -> None:
        decoder = FrameDecoder()
        original = _target(angle_deg=-20.0, distance_m=55.0, radial_velocity_mps=-8.0)

        frames = decoder.feed(build_frame([original]))

        assert len(frames) == 1
        assert len(frames[0]) == 1
        decoded = frames[0][0]
        assert decoded.angle_deg == pytest.approx(original.angle_deg)
        assert decoded.distance_m == pytest.approx(original.distance_m)
        assert decoded.radial_velocity_mps == pytest.approx(original.radial_velocity_mps, abs=0.3)
        assert decoded.signal_strength == original.signal_strength
        assert decoder.stats.frames_decoded == 1

    def test_several_frames_in_one_read(self) -> None:
        """A read returns whatever has buffered, which is frequently more than one frame."""
        decoder = FrameDecoder()
        stream = b"".join(build_frame([_target(distance_m=float(d))]) for d in (10, 20, 30))

        frames = decoder.feed(stream)

        assert [f[0].distance_m for f in frames] == [10.0, 20.0, 30.0]

    def test_a_frame_reporting_no_targets_is_preserved(self) -> None:
        """An empty target list is an observation — "nothing in view" — not missing data.

        Dropping it would make a working radar watching an empty sky indistinguishable from a
        radar that has stopped reporting (FM-010).
        """
        decoder = FrameDecoder()

        frames = decoder.feed(build_frame([]))

        assert frames == [[]]
        assert decoder.stats.frames_decoded == 1

    def test_multiple_targets_in_one_frame(self) -> None:
        decoder = FrameDecoder()
        targets = [
            _target(distance_m=float(d), angle_deg=float(a))
            for d, a in ((10, -30), (60, 15), (95, 40))
        ]

        frames = decoder.feed(build_frame(targets))

        assert len(frames[0]) == 3
        assert [t.distance_m for t in frames[0]] == [10.0, 60.0, 95.0]


class TestSignConvention:
    """The most consequential constant in the protocol, isolated.

    An inverted direction flag produces tracks that open when they are closing. Nothing
    downstream can detect that — the values are entirely plausible — so it is asserted here in
    the terms an operator would care about rather than left to a byte comparison.
    """

    def test_an_approaching_target_reports_negative_radial_velocity(self) -> None:
        decoder = FrameDecoder()

        frames = decoder.feed(build_frame([_target(radial_velocity_mps=-12.0)]))

        assert frames[0][0].radial_velocity_mps < 0

    def test_a_receding_target_reports_positive_radial_velocity(self) -> None:
        decoder = FrameDecoder()

        frames = decoder.feed(build_frame([_target(radial_velocity_mps=12.0)]))

        assert frames[0][0].radial_velocity_mps > 0

    def test_angles_either_side_of_boresight_keep_their_sign(self) -> None:
        decoder = FrameDecoder()

        left = decoder.feed(build_frame([_target(angle_deg=-25.0)]))[0][0]
        right = decoder.feed(build_frame([_target(angle_deg=25.0)]))[0][0]

        assert left.angle_deg < 0 < right.angle_deg


class TestResynchronisation:
    def test_leading_garbage_is_discarded_and_the_frame_still_decodes(self) -> None:
        """The first read of a running device almost never starts on a frame boundary."""
        decoder = FrameDecoder()

        frames = decoder.feed(b"\x11\x22\x33garbage" + build_frame([_target()]))

        assert len(frames) == 1
        assert decoder.stats.bytes_discarded > 0

    def test_a_corrupted_frame_is_rejected_and_the_next_one_recovered(self) -> None:
        """The critical property: one bad frame costs one frame, not the run."""
        decoder = FrameDecoder()
        good = build_frame([_target(distance_m=77.0)])
        corrupt = bytearray(build_frame([_target(distance_m=11.0)]))
        corrupt[-1] ^= 0xFF  # break the footer

        frames = decoder.feed(bytes(corrupt) + good)

        assert len(frames) == 1
        assert frames[0][0].distance_m == 77.0

    def test_a_dropped_byte_does_not_desynchronise_permanently(self) -> None:
        decoder = FrameDecoder()
        damaged = bytearray(build_frame([_target(distance_m=11.0)]))
        del damaged[6]

        decoder.feed(bytes(damaged))
        frames = decoder.feed(build_frame([_target(distance_m=88.0)]))

        assert [f[0].distance_m for f in frames] == [88.0]

    def test_a_header_sequence_inside_a_payload_does_not_derail_the_reader(self) -> None:
        """Payload bytes are arbitrary, so they will eventually spell the header.

        A reader that trusted the first header it found would mis-frame here and could stay
        mis-framed. Length and footer are validated together precisely so that a false header
        fails the check and the scan moves past it.
        """
        decoder = FrameDecoder()
        # Signal strength bytes chosen to reproduce the header sequence inside the payload.
        sneaky = [
            _target(signal_strength=0xF4),
            _target(signal_strength=0xF3),
            _target(signal_strength=0xF2),
            _target(signal_strength=0xF1),
        ]

        frames = decoder.feed(build_frame(sneaky) + build_frame([_target(distance_m=64.0)]))

        assert frames, "the reader lost both frames to a header sequence inside a payload"
        assert frames[-1][0].distance_m == 64.0


class TestPartialReads:
    def test_a_frame_split_across_two_reads(self) -> None:
        decoder = FrameDecoder()
        whole = build_frame([_target(distance_m=42.0)])

        assert decoder.feed(whole[:5]) == []
        frames = decoder.feed(whole[5:])

        assert [f[0].distance_m for f in frames] == [42.0]

    def test_a_frame_delivered_one_byte_at_a_time(self) -> None:
        """The pathological case, and the one a slow line actually produces."""
        decoder = FrameDecoder()
        whole = build_frame([_target(distance_m=33.0)])

        completed = []
        for index in range(len(whole)):
            completed.extend(decoder.feed(whole[index : index + 1]))

        assert len(completed) == 1
        assert completed[0][0].distance_m == 33.0

    def test_a_header_split_across_two_reads_is_not_lost(self) -> None:
        """The buffer keeps a trailing fragment for exactly this case."""
        decoder = FrameDecoder()
        whole = build_frame([_target(distance_m=21.0)])

        decoder.feed(b"\xaa\xbb" + whole[:2])
        frames = decoder.feed(whole[2:])

        assert [f[0].distance_m for f in frames] == [21.0]


class TestMalformedInput:
    def test_an_absurd_length_field_does_not_stall_the_reader(self) -> None:
        """A corrupt length can claim a payload that will never arrive.

        Without the ceiling the reader waits for those bytes forever, and a stalled reader
        reports an empty sky rather than a fault.
        """
        decoder = FrameDecoder()
        wire = WIRE_FORMAT
        poisoned = wire.header + (0xFFFF).to_bytes(wire.length_bytes, "little") + b"\x00" * 8

        decoder.feed(poisoned)
        frames = decoder.feed(build_frame([_target(distance_m=50.0)]))

        assert [f[0].distance_m for f in frames] == [50.0]

    def test_a_target_count_the_frame_cannot_substantiate_is_rejected(self) -> None:
        """Decoding the targets that happen to fit would report part of a known-bad frame."""
        decoder = FrameDecoder()
        wire = WIRE_FORMAT
        payload = bytearray(wire.header_target_count_bytes)
        payload[wire.header_target_count_bytes - 1] = 4  # claims four
        payload.extend(b"\x00" * wire.target_bytes)  # supplies one
        frame = (
            wire.header
            + len(payload).to_bytes(wire.length_bytes, "little")
            + bytes(payload)
            + wire.footer
        )

        assert decoder.feed(frame) == []
        assert decoder.stats.malformed_frames == 1

    def test_a_target_count_above_the_device_maximum_is_rejected(self) -> None:
        decoder = FrameDecoder()
        wire = WIRE_FORMAT
        count = wire.max_targets + 1
        payload = bytearray(wire.header_target_count_bytes)
        payload[wire.header_target_count_bytes - 1] = count
        payload.extend(b"\x00" * wire.target_bytes * count)
        frame = (
            wire.header
            + len(payload).to_bytes(wire.length_bytes, "little")
            + bytes(payload)
            + wire.footer
        )

        assert decoder.feed(frame) == []
        assert decoder.stats.malformed_frames == 1

    def test_continuous_garbage_does_not_grow_the_buffer_without_limit(self) -> None:
        """A wrong baud rate produces bytes that never contain a header.

        On an unattended system an unbounded buffer is a worse failure than dropped bytes.
        """
        decoder = FrameDecoder()

        for _ in range(200):
            decoder.feed(b"\x5a" * 256)

        assert len(decoder._buffer) <= WIRE_FORMAT.max_payload_bytes * 4  # noqa: SLF001
        assert decoder.stats.bytes_discarded > 0

    def test_a_read_larger_than_the_buffer_ceiling_loses_nothing(self) -> None:
        """The bound must never cost a complete frame.

        Trimming on the way in discards the front of the buffer, and the front of the buffer is
        the oldest *complete* frame. At 115200 baud a 0.2s read returns roughly 2300 bytes
        against a 2048-byte ceiling, so this was not an edge case — it fired on every full read
        and silently deleted several frames each time.
        """
        decoder = FrameDecoder()
        count = 500
        stream = b"".join(
            build_frame([_target(distance_m=float(index % 250))]) for index in range(count)
        )
        assert len(stream) > WIRE_FORMAT.max_payload_bytes * 4, "stream must exceed the ceiling"

        frames = decoder.feed(stream)

        assert len(frames) == count
        assert decoder.stats.bytes_discarded == 0

    def test_recovery_after_sustained_garbage(self) -> None:
        """Having survived the garbage, the reader must still work."""
        decoder = FrameDecoder()
        for _ in range(50):
            decoder.feed(b"\x5a" * 256)

        frames = decoder.feed(build_frame([_target(distance_m=17.0)]))

        assert [f[0].distance_m for f in frames] == [17.0]


class TestStatistics:
    def test_decoded_frames_are_counted(self) -> None:
        decoder = FrameDecoder()

        for _ in range(5):
            decoder.feed(build_frame([_target()]))

        assert decoder.stats.frames_decoded == 5

    def test_resync_events_are_counted_so_a_failing_line_is_visible(self) -> None:
        """An occasional resync is a normal serial line; a rising rate is a cable fault.

        Only a count over time distinguishes those, which is why this is measured rather than
        logged and forgotten.
        """
        decoder = FrameDecoder()

        decoder.feed(b"\x00" * 32 + build_frame([_target()]))

        assert decoder.stats.resync_events > 0


class TestWireFormatIsData:
    def test_an_alternative_wire_format_is_honoured_throughout(self) -> None:
        """Correcting a constant after a capture must be a data change, not a code change.

        This is what makes the bring-up procedure cheap: adjust the values, and every test in
        this file still applies to the corrected format.
        """
        custom = WireFormat(
            header=b"\xaa\xbb",
            footer=b"\xcc\xdd",
            speed_scale_mps=1.0,
            approaching_flag=0x02,
        )
        decoder = FrameDecoder(wire=custom)

        frames = decoder.feed(
            build_frame([_target(radial_velocity_mps=-9.0, distance_m=25.0)], wire=custom)
        )

        assert len(frames) == 1
        assert frames[0][0].distance_m == 25.0
        assert frames[0][0].radial_velocity_mps == pytest.approx(-9.0, abs=0.6)

    def test_frames_of_one_format_are_not_decoded_by_another(self) -> None:
        """Guards the bring-up failure mode: a stream that decodes as silence, not as an error.

        If the configured header does not match the device, every frame is discarded as
        garbage and the adapter reports an empty sky. That must show up as bytes discarded, so
        the bring-up procedure has something to look at.
        """
        decoder = FrameDecoder(wire=WireFormat(header=b"\x01\x02", footer=b"\x03\x04"))

        assert decoder.feed(build_frame([_target()])) == []
        assert decoder.stats.bytes_discarded > 0


class TestRepresentableEnvelope:
    """The wire format bounds the sensor's envelope, whatever the datasheet says.

    Every field is a single byte, so range, angle and speed each have a hard ceiling that no
    configuration can exceed. A configured envelope larger than the wire format would not fail
    loudly — it would wrap, and a distant target would be reported as a near one.
    """

    def test_the_configured_coverage_fits_inside_the_wire_format(self) -> None:
        """Guards the two files against drifting apart.

        `radar.coverage` and `WireFormat` are edited for different reasons by different people:
        one from a datasheet, one from a capture. Nothing else connects them.
        """
        from sensors.config.loader import load_sensor_config

        coverage = load_sensor_config().radar.coverage
        assert coverage is not None, "the radar has no configured coverage to check"

        range_min, range_max = WIRE_FORMAT.representable_range_m()
        angle_min, angle_max = WIRE_FORMAT.representable_angle_deg()
        _, speed_max = WIRE_FORMAT.representable_speed_mps()

        assert range_min <= coverage.min_range_m
        assert coverage.max_range_m <= range_max, (
            f"max_range_m={coverage.max_range_m} exceeds the {range_max} m the distance field "
            "can express; a target beyond it would wrap and be reported as a near contact"
        )
        assert coverage.max_speed_mps <= speed_max
        half_fov = coverage.horizontal_fov_deg / 2.0
        assert angle_min <= -half_fov and half_fov <= angle_max

    @pytest.mark.parametrize(
        ("field", "target"),
        [
            ("distance_m", RawTarget(0.0, 300.0, 1.0, 100)),
            ("angle_deg", RawTarget(200.0, 10.0, 1.0, 100)),
            ("radial_velocity_mps", RawTarget(0.0, 10.0, 200.0, 100)),
        ],
    )
    def test_an_unrepresentable_target_is_named_not_just_rejected(
        self, field: str, target: RawTarget
    ) -> None:
        """"byte must be in range(0, 256)" says nothing about which field or what the limit is."""
        with pytest.raises(ProtocolError, match=field):
            build_frame([target])

    def test_the_envelope_boundaries_themselves_round_trip(self) -> None:
        """The limits are inclusive, so the edge of the envelope must still encode."""
        _, range_max = WIRE_FORMAT.representable_range_m()
        angle_min, angle_max = WIRE_FORMAT.representable_angle_deg()

        decoder = FrameDecoder()
        frames = decoder.feed(
            build_frame(
                [
                    RawTarget(angle_min, range_max, 0.0, 255),
                    RawTarget(angle_max, 0.0, 0.0, 0),
                ]
            )
        )

        assert [t.distance_m for t in frames[0]] == [range_max, 0.0]
        assert [t.angle_deg for t in frames[0]] == [angle_min, angle_max]
