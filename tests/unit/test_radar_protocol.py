"""Frame decoding, against bytes rather than against hardware.

Every test here builds the bytes the device would send and asserts on what comes out, which is
what makes it possible to have this adapter under test on a machine with no serial port. The
awkward cases — a stream joined mid-frame, a header that is a coincidence, a frame split
across two reads — are the ones that actually happen on a link that has just been plugged in,
so they are the ones with tests.
"""

from __future__ import annotations

import pytest

from sensors.adapters.radar.protocol import (
    COMMAND_FRAME_FOOTER,
    COMMAND_FRAME_HEADER,
    TARGET_FRAME_FOOTER,
    TARGET_FRAME_HEADER,
    FrameDecoder,
    FrameKind,
    ProtocolError,
    build_command_frame,
    decode_target_frame,
)


def target_frame(*targets: tuple[int, int, int, int, int], alarm: int = 1) -> bytes:
    """Assemble a target-data frame the way the device would.

    Each target is `(azimuth_raw, range_m, direction, speed_kmh, signal)` — raw bytes, so a
    test can express a malformed frame as easily as a well-formed one.
    """
    payload = bytes((alarm, len(targets)))
    for target in targets:
        payload += bytes(target)
    return (
        TARGET_FRAME_HEADER
        + len(payload).to_bytes(2, "little")
        + payload
        + TARGET_FRAME_FOOTER
    )


def test_a_single_frame_decodes_to_its_payload() -> None:
    decoder = FrameDecoder()

    frames = decoder.feed(target_frame((0x80, 30, 1, 18, 50)))

    assert len(frames) == 1
    assert frames[0].kind is FrameKind.TARGET_DATA
    assert decoder.stats.bytes_discarded == 0


def test_a_frame_split_across_reads_is_reassembled() -> None:
    """The port hands back whatever has arrived, which is rarely a whole frame."""
    raw = target_frame((0x85, 42, 0, 9, 33))
    decoder = FrameDecoder()

    for index in range(len(raw) - 1):
        assert decoder.feed(raw[index : index + 1]) == []
    frames = decoder.feed(raw[-1:])

    assert len(frames) == 1
    assert decoder.stats.bytes_discarded == 0


def test_a_stream_joined_mid_frame_resynchronises_on_the_next_one() -> None:
    """Opening a port mid-transmission delivers the tail of whatever was in flight."""
    raw = target_frame((0x80, 10, 1, 5, 20)) + target_frame((0x88, 20, 0, 6, 21))
    decoder = FrameDecoder()

    frames = decoder.feed(raw[7:])

    assert len(frames) == 1
    assert decoder.stats.bytes_discarded > 0
    assert decode_target_frame(frames[0].payload).targets[0].range_m == 20.0


def test_a_coincidental_header_costs_one_byte_not_the_buffer() -> None:
    """A header appearing inside noise must not swallow the good frame behind it.

    Dropping the whole buffer on a failed footer check is the obvious implementation and it
    loses a frame every time noise happens to contain F4 F3 F2 F1 — on a link that is
    resynchronising, that is often.
    """
    good = target_frame((0x80, 15, 1, 12, 40))
    decoder = FrameDecoder()

    frames = decoder.feed(TARGET_FRAME_HEADER + b"\x02\x00\x00\x00" + good)

    assert len(frames) == 1
    assert decode_target_frame(frames[0].payload).targets[0].range_m == 15.0


def test_command_replies_decode_as_their_own_kind() -> None:
    """The device answers configuration commands in a different framing on the same wire."""
    decoder = FrameDecoder()

    frames = decoder.feed(build_command_frame(0x00FF))

    assert [frame.kind for frame in frames] == [FrameKind.COMMAND_REPLY]


def test_an_absurd_length_field_does_not_stall_the_decoder() -> None:
    """A garbage length must not park the decoder waiting for bytes that will never come."""
    decoder = FrameDecoder()
    good = target_frame((0x80, 25, 1, 8, 30))

    frames = decoder.feed(TARGET_FRAME_HEADER + b"\xff\xff" + good)

    assert len(frames) == 1


def test_azimuth_is_signed_around_the_bias() -> None:
    decoded = decode_target_frame(
        target_frame((0x80, 10, 1, 0, 10), (0x75, 10, 1, 0, 10), (0x8B, 10, 1, 0, 10))[6:-4]
    )

    assert [target.azimuth_deg for target in decoded.targets] == [0.0, -11.0, 11.0]


def test_speed_is_metres_per_second_and_signed_by_direction() -> None:
    """The device sends unsigned km/h and a direction flag; the schema wants signed m/s."""
    decoded = decode_target_frame(
        target_frame((0x80, 30, 1, 36, 50), (0x80, 30, 0, 36, 50))[6:-4], approaching_code=1
    )
    approaching, receding = decoded.targets

    assert approaching.speed_mps == pytest.approx(10.0)
    assert approaching.radial_velocity_mps == pytest.approx(-10.0)
    assert receding.radial_velocity_mps == pytest.approx(10.0)


def test_the_direction_convention_is_configurable() -> None:
    """Published descriptions of this device disagree on which code means approaching.

    The mapping is therefore configuration, and this test is what stops it quietly becoming a
    constant again — the sign of every range rate the adapter emits depends on it.
    """
    payload = target_frame((0x80, 30, 1, 36, 50))[6:-4]

    assert decode_target_frame(payload, approaching_code=1).targets[0].radial_velocity_mps < 0
    assert decode_target_frame(payload, approaching_code=0).targets[0].radial_velocity_mps > 0


def test_an_empty_report_is_a_frame_and_not_a_dropped_one() -> None:
    """A report with no targets is how a working sensor says the sky is empty (FM-010)."""
    decoded = decode_target_frame(target_frame(alarm=0)[6:-4])

    assert decoded.targets == ()
    assert decoded.alarm == 0


def test_a_zero_length_payload_is_also_an_empty_report() -> None:
    """Observed on real hardware: some units send no alarm/count bytes at all when idle.

    Header, a length field of zero, footer, nothing in between. It means exactly what the
    alarm-byte-plus-zero-count form means on other units, and must decode the same way rather
    than being rejected as malformed — a device sitting there working correctly would
    otherwise look like a stream of protocol errors on every idle cycle.
    """
    decoded = decode_target_frame(b"")

    assert decoded.targets == ()
    assert decoded.alarm == 0


def test_a_target_count_the_payload_cannot_support_is_rejected() -> None:
    payload = bytes((1, 3)) + bytes(5)

    with pytest.raises(ProtocolError, match="claims 3 targets"):
        decode_target_frame(payload)


def test_command_framing_round_trips() -> None:
    frame = build_command_frame(0x00FF, b"\x01\x00")

    assert frame.startswith(COMMAND_FRAME_HEADER)
    assert frame.endswith(COMMAND_FRAME_FOOTER)
    assert FrameDecoder().feed(frame)[0].payload == b"\xff\x00\x01\x00"
