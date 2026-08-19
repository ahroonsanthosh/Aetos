"""Wire protocol parsing for the UART FMCW radar adapter.

Pure byte-sequence tests — no serial port, no `pyserial`, no hardware. `extract_data_frames`
and `parse_targets` are the only load-bearing logic between "bytes arrived on a wire" and "a
target record adapter.py can turn into a Detection", so framing correctness is tested
directly rather than trusted.
"""

from __future__ import annotations

import pytest

from sensors.adapters.uart_fmcw_radar.protocol import (
    DATA_FRAME_FOOTER,
    DATA_FRAME_HEADER,
    FrameError,
    extract_data_frames,
    parse_targets,
)


def _target_record(
    *, angle_byte: int = 0x80, distance_byte: int = 10, direction_byte: int = 0x00,
    speed_byte: int = 0, signal_byte: int = 100,
) -> bytes:
    return bytes([angle_byte, distance_byte, direction_byte, speed_byte, signal_byte])


def _frame(payload: bytes) -> bytes:
    return DATA_FRAME_HEADER + len(payload).to_bytes(2, "little") + payload + DATA_FRAME_FOOTER


def _payload(*records: bytes, count: int | None = None) -> bytes:
    header = bytes([count if count is not None else len(records), 0x00])
    return header + b"".join(records)


def test_extract_single_complete_frame() -> None:
    payload = _payload(_target_record())
    frames, remainder = extract_data_frames(_frame(payload))

    assert frames == [payload]
    assert remainder == b""


def test_extract_returns_empty_when_no_header_present() -> None:
    frames, remainder = extract_data_frames(b"\x01\x02\x03garbage")

    assert frames == []
    # Only the last few bytes are retained, in case a header started near the end.
    assert len(remainder) <= 3


def test_extract_skips_leading_garbage_before_a_header() -> None:
    payload = _payload(_target_record())
    frames, remainder = extract_data_frames(b"\x00\x01\xff" + _frame(payload))

    assert frames == [payload]
    assert remainder == b""


def test_extract_buffers_a_frame_split_across_two_calls() -> None:
    payload = _payload(_target_record())
    whole = _frame(payload)
    first_half, second_half = whole[:7], whole[7:]

    frames, remainder = extract_data_frames(first_half)
    assert frames == []
    assert remainder == first_half

    frames, remainder = extract_data_frames(remainder + second_half)
    assert frames == [payload]
    assert remainder == b""


def test_extract_resyncs_past_a_corrupt_length_field() -> None:
    """A header-shaped byte sequence whose length field does not lead to a real footer must
    not swallow a genuine frame that follows it."""
    payload = _payload(_target_record())
    good_frame = _frame(payload)

    # A header followed by a length field that overshoots into unrelated bytes, with no real
    # footer where that length implies one — then a real, well-formed frame right after.
    bogus = DATA_FRAME_HEADER + (5).to_bytes(2, "little") + b"NOTAFOOTR"
    buffer = bogus + good_frame

    frames, remainder = extract_data_frames(buffer)

    assert frames == [payload]
    assert remainder == b""


def test_extract_finds_multiple_frames_in_one_buffer() -> None:
    payload_a = _payload(_target_record(distance_byte=5))
    payload_b = _payload(_target_record(distance_byte=50))

    frames, remainder = extract_data_frames(_frame(payload_a) + _frame(payload_b))

    assert frames == [payload_a, payload_b]
    assert remainder == b""


def test_parse_targets_reads_angle_distance_direction_speed_signal() -> None:
    payload = _payload(
        _target_record(
            angle_byte=0x94,  # 0x80 + 20 -> +20 degrees
            distance_byte=42,
            direction_byte=0x01,
            speed_byte=30,
            signal_byte=200,
        )
    )

    targets = parse_targets(payload)

    assert len(targets) == 1
    target = targets[0]
    assert target.angle_deg == 20.0
    assert target.distance_m == 42.0
    assert target.direction_byte == 0x01
    assert target.speed_kph == 30.0
    assert target.signal_raw == 200


def test_parse_targets_angle_byte_0x80_is_zero_degrees() -> None:
    payload = _payload(_target_record(angle_byte=0x80))
    assert parse_targets(payload)[0].angle_deg == 0.0


def test_parse_targets_angle_byte_below_0x80_is_negative() -> None:
    payload = _payload(_target_record(angle_byte=0x6C))  # 0x80 - 20
    assert parse_targets(payload)[0].angle_deg == -20.0


def test_parse_targets_handles_multiple_records_in_one_payload() -> None:
    payload = _payload(
        _target_record(distance_byte=10),
        _target_record(distance_byte=20),
        _target_record(distance_byte=30),
    )

    targets = parse_targets(payload)

    assert [t.distance_m for t in targets] == [10.0, 20.0, 30.0]


def test_parse_targets_empty_when_count_header_has_no_records() -> None:
    assert parse_targets(_payload(count=0)) == []


def test_parse_targets_drops_a_trailing_incomplete_record_rather_than_raising() -> None:
    payload = _payload(_target_record()) + b"\x01\x02\x03"  # 3 stray trailing bytes

    targets = parse_targets(payload)

    assert len(targets) == 1


def test_parse_targets_raises_frame_error_when_payload_too_short_for_a_header() -> None:
    with pytest.raises(FrameError):
        parse_targets(b"\x00")
