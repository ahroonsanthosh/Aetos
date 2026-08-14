"""Framing and decoding for the radar testbed unit.

The protocol constants this decodes with are unverified against the vendor document, so these
tests deliberately do not assert that the constants are *right* — they cannot. What they assert
is that the decoder behaves correctly *given* them, and, more importantly, that it fails in a
diagnosable way when they are wrong: discarded bytes and malformed frames are counted, never
silently absorbed, so a wrong baud rate produces a distinctive signature rather than an
apparently quiet radar.
"""

from __future__ import annotations

import pytest

from sensors.radar.ld2451_protocol import (
    COMMAND_FRAME_HEADER,
    COMMAND_FRAME_TAIL,
    DATA_FRAME_HEADER,
    DATA_FRAME_TAIL,
    MAX_BUFFER_BYTES,
    FrameDecoder,
    FrameKind,
    ProtocolError,
    build_command,
    enable_config_command,
    parse_command_reply,
    parse_target_frame,
    set_sensitivity_command,
    set_target_detection_command,
)


def data_frame(payload: bytes) -> bytes:
    return DATA_FRAME_HEADER + len(payload).to_bytes(2, "little") + payload + DATA_FRAME_TAIL


def target_payload(*targets: tuple[int, int, int, int, int]) -> bytes:
    """Assemble a target-report payload: count, alarm byte, then five bytes per target."""
    body = bytes((len(targets), 0x00))
    for angle, distance, direction, speed, signal in targets:
        body += bytes((angle, distance, direction, speed, signal))
    return body


class TestFraming:
    def test_a_single_whole_frame_decodes(self) -> None:
        decoder = FrameDecoder()
        frames = decoder.feed(data_frame(target_payload((0x80, 25, 1, 36, 200))))

        assert len(frames) == 1
        assert frames[0].kind is FrameKind.DATA
        assert decoder.bytes_discarded == 0
        assert decoder.malformed_frames == 0

    def test_a_frame_split_across_reads_decodes_once_complete(self) -> None:
        """Serial reads have no relationship to frame boundaries. This is the normal case, not
        an edge case: at 115200 baud a frame routinely straddles two reads."""
        whole = data_frame(target_payload((0x85, 40, 0, 10, 120)))
        decoder = FrameDecoder()

        for cut in range(1, len(whole)):
            decoder = FrameDecoder()
            assert decoder.feed(whole[:cut]) == []
            frames = decoder.feed(whole[cut:])
            assert len(frames) == 1, f"frame split at byte {cut} failed to decode"

    def test_several_frames_in_one_read_all_decode_in_order(self) -> None:
        first = data_frame(target_payload((0x80, 10, 1, 5, 90)))
        second = data_frame(target_payload((0x90, 20, 0, 15, 80)))

        frames = FrameDecoder().feed(first + second)

        assert [parse_target_frame(f.payload)[0].distance_m for f in frames] == [10.0, 20.0]

    def test_leading_garbage_is_discarded_and_counted(self) -> None:
        """Starting mid-stream is the normal case on connect: the unit was already talking."""
        decoder = FrameDecoder()
        frames = decoder.feed(b"\x11\x22\x33" + data_frame(target_payload()))

        assert len(frames) == 1
        assert decoder.bytes_discarded == 3

    def test_a_bad_tail_is_counted_as_malformed_and_the_stream_resynchronises(self) -> None:
        """The signature of a wrong protocol assumption. It must be counted, and the decoder
        must recover on the next good frame rather than stalling forever."""
        payload = target_payload((0x80, 5, 1, 3, 50))
        header = DATA_FRAME_HEADER + len(payload).to_bytes(2, "little")
        corrupt = header + payload + b"\x00\x00\x00\x00"
        good = data_frame(target_payload((0x82, 7, 0, 4, 60)))

        decoder = FrameDecoder()
        frames = decoder.feed(corrupt + good)

        assert len(frames) == 1
        assert decoder.malformed_frames == 1
        assert parse_target_frame(frames[0].payload)[0].distance_m == 7.0

    def test_an_absurd_length_does_not_stall_the_decoder(self) -> None:
        """A misread header can declare a payload of thousands of bytes. Waiting for them would
        park the decoder permanently on noise, which looks exactly like a quiet radar."""
        absurd = DATA_FRAME_HEADER + (60_000).to_bytes(2, "little") + b"\x00\x00"
        good = data_frame(target_payload())

        decoder = FrameDecoder()
        frames = decoder.feed(absurd + good)

        assert len(frames) == 1
        assert decoder.malformed_frames == 1

    def test_a_header_appearing_inside_a_payload_does_not_break_the_next_frame(self) -> None:
        """Payload bytes are arbitrary and can spell a header. Resynchronisation steps forward
        one byte at a time for exactly this reason."""
        decoder = FrameDecoder()
        stream = DATA_FRAME_HEADER[:3] + data_frame(target_payload((0x80, 9, 1, 2, 3)))
        frames = decoder.feed(stream)

        assert len(frames) == 1
        assert parse_target_frame(frames[0].payload)[0].distance_m == 9.0

    def test_a_header_split_across_reads_is_not_destroyed_by_the_sync_hunt(self) -> None:
        """The hunt for sync must retain a possible partial header at the buffer's end, or it
        would discard the very bytes it is looking for."""
        whole = data_frame(target_payload())
        decoder = FrameDecoder()

        assert decoder.feed(whole[:2]) == []
        assert len(decoder.feed(whole[2:])) == 1

    def test_command_replies_decode_from_the_same_stream_as_target_data(self) -> None:
        """Configuration replies arrive interleaved with target frames. A decoder that knew only
        about data frames would count every reply as noise."""
        body = (0x01FF).to_bytes(2, "little") + (0x0000).to_bytes(2, "little")
        reply = COMMAND_FRAME_HEADER + len(body).to_bytes(2, "little") + body + COMMAND_FRAME_TAIL

        frames = FrameDecoder().feed(data_frame(target_payload()) + reply)

        assert [f.kind for f in frames] == [FrameKind.DATA, FrameKind.COMMAND_REPLY]
        assert parse_command_reply(frames[1].payload).succeeded

    def test_a_stream_that_never_frames_keeps_memory_bounded(self) -> None:
        """The wrong-baud-rate case. Memory must stay bounded and the discard count must climb —
        that count is the only evidence of what is happening."""
        decoder = FrameDecoder()
        decoder.feed(b"\x5a" * (MAX_BUFFER_BYTES * 3))

        assert decoder.buffered_bytes <= MAX_BUFFER_BYTES
        assert decoder.bytes_discarded > MAX_BUFFER_BYTES

    def test_reset_drops_buffered_bytes_but_keeps_the_session_counters(self) -> None:
        """Called on reconnect: the half-frame from a dead link is not part of the first frame
        after recovery, but the counters describe the session and must survive."""
        decoder = FrameDecoder()
        decoder.feed(b"\x00\x00\x00" + DATA_FRAME_HEADER)
        discarded = decoder.bytes_discarded

        decoder.reset()

        assert decoder.buffered_bytes == 0
        assert decoder.bytes_discarded > discarded


class TestTargetDecoding:
    def test_an_empty_target_frame_is_valid_and_reports_nothing(self) -> None:
        """A radar in an empty sky still emits frames. Zero targets is data, not silence."""
        assert parse_target_frame(target_payload()) == []

    def test_fields_decode_in_the_units_the_hardware_uses(self) -> None:
        reports = parse_target_frame(target_payload((0x80 + 12, 47, 1, 36, 210)))

        assert reports[0].angle_deg == 12.0
        assert reports[0].distance_m == 47.0
        assert reports[0].approaching is True
        assert reports[0].speed_kmh == 36.0
        assert reports[0].signal_level == 210

    def test_a_negative_angle_decodes_through_the_zero_offset(self) -> None:
        """The angle byte is unsigned with zero at 0x80, so half the beam is below the offset."""
        assert parse_target_frame(target_payload((0x80 - 15, 30, 0, 0, 100)))[0].angle_deg == -15.0

    @pytest.mark.parametrize(
        ("direction", "speed_kmh", "expected_mps"),
        [
            (1, 36, -10.0),  # approaching: range decreasing, so range rate is negative
            (0, 36, 10.0),  # receding
            (1, 0, 0.0),  # a hovering target has no range rate whatever its direction flag
        ],
    )
    def test_speed_and_direction_become_a_signed_range_rate(
        self, direction: int, speed_kmh: int, expected_mps: float
    ) -> None:
        """The canonical schema carries a signed range rate, positive receding. Applying the sign
        once, here, avoids a second field that some later caller forgets to consult."""
        report = parse_target_frame(target_payload((0x80, 20, direction, speed_kmh, 128)))[0]
        assert report.radial_velocity_mps == pytest.approx(expected_mps)

    def test_multiple_targets_decode_at_their_own_offsets(self) -> None:
        reports = parse_target_frame(
            target_payload((0x80, 10, 1, 18, 200), (0x8A, 55, 0, 72, 90), (0x76, 99, 1, 0, 30))
        )

        assert [r.distance_m for r in reports] == [10.0, 55.0, 99.0]
        assert [r.angle_deg for r in reports] == [0.0, 10.0, -10.0]

    def test_a_payload_shorter_than_its_own_target_count_is_rejected_whole(self) -> None:
        """Not partially decoded. A payload whose length disagrees with its count has field
        offsets nobody can trust, and half-decoding it would invent targets at fabricated
        ranges."""
        truncated = bytes((3, 0x00)) + bytes((0x80, 10, 1, 5, 90))

        with pytest.raises(ProtocolError, match="declares 3 target"):
            parse_target_frame(truncated)

    def test_a_payload_too_short_to_carry_a_count_is_rejected(self) -> None:
        with pytest.raises(ProtocolError, match="too short"):
            parse_target_frame(b"\x01")

    def test_trailing_bytes_beyond_the_declared_targets_are_rejected(self) -> None:
        """An over-long payload means the layout assumption is wrong just as surely as a short
        one does, and the returns in it are no more trustworthy."""
        with pytest.raises(ProtocolError):
            parse_target_frame(target_payload((0x80, 10, 1, 5, 90)) + b"\xff\xff")


class TestCommandFrames:
    def test_a_built_command_round_trips_through_the_decoder(self) -> None:
        frames = FrameDecoder().feed(build_command(0x00A0, b"\x01\x02"))

        assert frames[0].kind is FrameKind.COMMAND_REPLY
        assert frames[0].payload == (0x00A0).to_bytes(2, "little") + b"\x01\x02"

    def test_the_command_word_is_little_endian(self) -> None:
        """The one assumption most likely to be wrong, asserted directly rather than implied by a
        round trip through code that shares the assumption."""
        assert build_command(0x00FF)[6:8] == b"\xff\x00"

    def test_tuning_commands_carry_the_values_they_were_given(self) -> None:
        command = set_target_detection_command(
            max_distance_m=60, min_speed_kmh=4, no_target_delay_s=2
        )

        assert command[8] == 60
        assert command[10] == 4
        assert command[11] == 2

    def test_sensitivity_command_carries_both_tunables(self) -> None:
        command = set_sensitivity_command(min_confirm_frames=3, sensitivity=7)

        assert command[8] == 3
        assert command[9] == 7

    def test_the_enable_config_handshake_is_a_well_formed_frame(self) -> None:
        command = enable_config_command()

        assert command.startswith(COMMAND_FRAME_HEADER)
        assert command.endswith(COMMAND_FRAME_TAIL)
        assert int.from_bytes(command[4:6], "little") == len(command) - 10

    def test_a_nonzero_reply_status_is_not_success(self) -> None:
        """Configuration commands are checked, not assumed. A rejected setting that looked like
        an accepted one would produce a calibration sweep whose runs all secretly used the same
        value."""
        payload = (0x01FF).to_bytes(2, "little") + (0x0001).to_bytes(2, "little")

        assert parse_command_reply(payload).succeeded is False

    def test_a_truncated_reply_is_rejected(self) -> None:
        with pytest.raises(ProtocolError, match="too short"):
            parse_command_reply(b"\xff\x01")
