"""Framing and decoding for the serial-attached FMCW radar.

The byte stream from a serial link is untrusted input. These tests are mostly about what the
parser does with rubbish: a link at the wrong baud rate, a half-arrived frame, a header magic
that appears inside noise. The requirement in every case is the same — keep framing, count
what was thrown away, and never raise past the parser, because an adapter that dies on a
corrupt frame is a sensor that goes silent the first time a jumper wire is nudged.
"""

from __future__ import annotations

import pytest

from sensors.adapters.uart_fmcw_radar.protocol import (
    COMMAND_HEADER,
    COMMAND_TAIL,
    REPORT_HEADER,
    REPORT_TAIL,
    Command,
    FrameError,
    ReportParser,
    build_command,
    parse_report_payload,
)


def frame(payload: bytes) -> bytes:
    return REPORT_HEADER + len(payload).to_bytes(2, "little") + payload + REPORT_TAIL


def target_record(azimuth: int, distance: int, direction: int, speed: int, quality: int) -> bytes:
    return bytes((azimuth, distance, direction, speed, quality))


ONE_TARGET = bytes((1,)) + target_record(0x85, 42, 1, 36, 90)


def test_count_first_payload_decodes() -> None:
    report = parse_report_payload(ONE_TARGET)

    assert report.alarm is None
    (target,) = report.targets
    assert target.azimuth_deg == 5.0
    assert target.range_m == 42.0
    assert target.approaching is True
    assert target.speed_kmh == 36


def test_alarm_then_count_payload_decodes() -> None:
    """The alternative layout is accepted while the vendor PDF is unconfirmed."""
    report = parse_report_payload(bytes((0x02, 1)) + target_record(0x7B, 10, 0, 5, 20))

    assert report.alarm == 0x02
    (target,) = report.targets
    assert target.azimuth_deg == -5.0
    assert target.range_m == 10.0


def test_radial_velocity_sign_follows_the_canonical_convention() -> None:
    """Negative is closing, positive is opening — the schema's convention, not the sensor's.

    The sensor reports an unsigned magnitude and a direction flag. Getting this backwards
    inverts every approach-versus-departure judgement risk makes downstream.
    """
    approaching = parse_report_payload(bytes((1,)) + target_record(0x80, 30, 1, 36, 50)).targets[0]
    receding = parse_report_payload(bytes((1,)) + target_record(0x80, 30, 0, 36, 50)).targets[0]

    assert approaching.radial_velocity_mps == pytest.approx(-10.0)
    assert receding.radial_velocity_mps == pytest.approx(10.0)


def test_payload_matching_no_known_layout_is_rejected() -> None:
    with pytest.raises(FrameError):
        parse_report_payload(bytes((4,)) + b"\x00\x00\x00")


def test_empty_payload_is_rejected() -> None:
    with pytest.raises(FrameError):
        parse_report_payload(b"")


def test_parser_decodes_a_whole_frame() -> None:
    parser = ReportParser()

    decoded = parser.feed(frame(ONE_TARGET))

    assert len(decoded) == 1
    assert len(decoded[0].targets) == 1
    assert parser.stats.frames_decoded == 1
    assert parser.stats.bytes_discarded == 0


def test_frame_split_across_reads_is_reassembled() -> None:
    """Serial reads split frames anywhere; the parser buffers across calls."""
    parser = ReportParser()
    whole = frame(ONE_TARGET)

    for cut in range(1, len(whole)):
        parser = ReportParser()
        assert parser.feed(whole[:cut]) == []
        decoded = parser.feed(whole[cut:])
        assert len(decoded) == 1, f"lost the frame when split at byte {cut}"


def test_leading_noise_is_counted_and_skipped() -> None:
    parser = ReportParser()

    decoded = parser.feed(b"\x11\x22\x33" + frame(ONE_TARGET))

    assert len(decoded) == 1
    assert parser.stats.bytes_discarded == 3
    assert parser.stats.consecutive_bytes_discarded == 0, "a good frame resets the run"


def test_pure_noise_never_syncs_and_says_so() -> None:
    """The signature of a wrong baud rate: bytes arrive, nothing frames.

    This is what stops a mis-configured link from looking like a working sensor observing an
    empty sky (FM-050).
    """
    parser = ReportParser()

    assert parser.feed(bytes(range(256)) * 4) == []
    assert parser.stats.frames_decoded == 0
    assert parser.stats.consecutive_bytes_discarded > 32


def test_a_false_header_inside_noise_does_not_desynchronise_the_next_frame() -> None:
    parser = ReportParser()

    decoded = parser.feed(REPORT_HEADER + b"\xff\xff" + b"\x00" * 4 + frame(ONE_TARGET))

    assert len(decoded) == 1, "a bogus length field must not swallow the frame behind it"
    assert parser.stats.frames_malformed >= 1


def test_a_frame_with_a_wrong_tail_is_discarded_not_raised() -> None:
    parser = ReportParser()
    length = len(ONE_TARGET).to_bytes(2, "little")
    corrupt = REPORT_HEADER + length + ONE_TARGET + b"\x00\x00\x00\x00"

    decoded = parser.feed(corrupt + frame(ONE_TARGET))

    assert len(decoded) == 1
    assert parser.stats.frames_malformed >= 1


def test_a_malformed_payload_does_not_stop_the_stream() -> None:
    parser = ReportParser()

    decoded = parser.feed(frame(bytes((9,)) + b"\x00\x00") + frame(ONE_TARGET))

    assert len(decoded) == 1
    assert parser.stats.frames_malformed == 1
    assert parser.stats.frames_decoded == 1


def test_buffer_does_not_grow_without_bound_under_noise() -> None:
    parser = ReportParser()

    for _ in range(100):
        parser.feed(b"\x01\x02\x03\x04\x05\x06\x07\x08")

    # Everything but a possible partial header is dropped as it arrives.
    assert parser.stats.bytes_discarded >= 800 - len(REPORT_HEADER)


def test_reset_drops_buffered_bytes() -> None:
    """Bytes from before a link drop cannot be assumed to join up with bytes from after it."""
    parser = ReportParser()
    whole = frame(ONE_TARGET)
    parser.feed(whole[:6])

    parser.reset()

    assert parser.feed(whole[6:]) == []


def test_command_framing_uses_the_command_magic() -> None:
    command = build_command(Command.ENABLE_CONFIG, b"\x01\x00")

    assert command.startswith(COMMAND_HEADER)
    assert command.endswith(COMMAND_TAIL)
    assert command[4:6] == (4).to_bytes(2, "little"), "length is little-endian"
    assert command[6:8] == (0x00FF).to_bytes(2, "little"), "command word is little-endian"
