"""Identifier generation: determinism, scoping and format stability."""

from __future__ import annotations

import pytest

from backend.common.identifiers import (
    IdGenerator,
    detection_id_generator,
    event_id_generator,
    is_valid_track_id,
    track_id_generator,
)


def test_identifiers_are_sequential_and_zero_padded() -> None:
    generator = track_id_generator()

    assert [generator.next() for _ in range(3)] == ["TRK-00001", "TRK-00002", "TRK-00003"]


def test_two_generators_produce_identical_sequences() -> None:
    """Determinism: a replayed scenario must produce the same identifiers as the original
    run, otherwise replay output cannot be compared against it. See ADR 0008."""
    assert [track_id_generator().next() for _ in range(5)] == [
        track_id_generator().next() for _ in range(5)
    ]


def test_generators_do_not_share_state() -> None:
    """Two scenarios in one process must not interleave their counters."""
    first, second = track_id_generator(), track_id_generator()

    first.next()
    first.next()

    assert second.next() == "TRK-00001"


def test_detection_ids_are_scoped_per_sensor() -> None:
    """Adding a sensor to a scenario must not shift the identifiers the others produce."""
    radar = detection_id_generator("SIM-RADAR-01")
    camera = detection_id_generator("SIM-EO-02")

    assert radar.next() == "det-simradar01-000001"
    assert camera.next() == "det-simeo02-000001"
    assert radar.next() == "det-simradar01-000002"


def test_sensor_id_separators_are_stripped_from_scope() -> None:
    """Sensor IDs come from configuration and, later, from hardware, so they are not trusted
    to be free of characters that would make the identifier ambiguous."""
    assert detection_id_generator("SIM/RADAR 01_A").next() == "det-simradar01a-000001"


def test_sensor_id_with_no_usable_characters_is_rejected() -> None:
    with pytest.raises(ValueError, match="no usable characters"):
        detection_id_generator("///")


def test_event_ids_are_wider_than_track_ids() -> None:
    """Events vastly outnumber tracks, so the padding differs. Note that the event log's
    ordering authority is its monotonic seq, not this identifier. See ADR 0006."""
    assert event_id_generator().next() == "evt-00000001"


def test_counter_rolls_past_padding_width_without_collision() -> None:
    """Padding is presentation, not a bound. Exceeding it must widen rather than wrap."""
    generator = IdGenerator("TRK", width=2, start=99)

    assert [generator.next() for _ in range(3)] == ["TRK-99", "TRK-100", "TRK-101"]


@pytest.mark.parametrize("value", ["TRK-00001", "TRK-123456"])
def test_valid_track_ids_are_recognised(value: str) -> None:
    assert is_valid_track_id(value)


@pytest.mark.parametrize(
    "value", ["TRK-1", "trk-00001", "TRACK-00001", "TRK00001", "", "det-000001"]
)
def test_malformed_track_ids_are_rejected(value: str) -> None:
    assert not is_valid_track_id(value)
