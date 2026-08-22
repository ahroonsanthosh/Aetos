"""The wire codec, tested against the cases a live unit will actually produce.

Written before any hardware is connected, and that is the point. A parser tested only against
captured samples is tested against whatever somebody happened to capture; with an encoder to
build frames and a way to corrupt a chosen byte, every case the format admits can be exercised
— including the ones that are hard to provoke on a bench and certain to happen on a cable.

The corruption tests matter most. This protocol has no checksum (ADR 0018), so the plausibility
envelope *is* the integrity check, and these are the tests that establish it does the job the
declared residual probability claims for it.
"""

from __future__ import annotations

import random

import pytest

from sensors.adapters.ld2451.protocol import (
    ANGLE_OFFSET,
    COMMAND_FRAME_HEADER,
    COMMAND_FRAME_TAIL,
    DATA_FRAME_HEADER,
    DATA_FRAME_TAIL,
    FrameDecoder,
    PlausibilityEnvelope,
    RejectReason,
    TargetFrame,
    TargetReport,
    assumed_altitude_sigma_m,
    encode_target_frame,
    residual_corruption_probability,
)

ENVELOPE = PlausibilityEnvelope(
    max_azimuth_deg=25,
    min_distance_m=0,
    max_distance_m=100,
    max_speed_kmh=200,
    min_snr=0,
    max_snr=100,
    max_targets=10,
)
"""The device's rated aperture with a little margin, its full single-byte distance range, and a
deliberately loose speed ceiling — chosen to match the worked example in ADR 0018 so the
residual figures in that record can be checked against this suite."""

TIGHT_ENVELOPE = ENVELOPE.model_copy(update={"max_speed_kmh": 80})
"""The same device with a speed ceiling a site would actually declare. Used to show that
tightening the envelope measurably improves link integrity, which is the operator's only lever
on a protocol with no checksum."""


def target(**overrides) -> TargetReport:
    defaults = {
        "angle_deg": 10,
        "distance_m": 40,
        "approaching": False,
        "speed_kmh": 60,
        "snr": 21,
    }
    return TargetReport(**{**defaults, **overrides})


def decoder(envelope: PlausibilityEnvelope = ENVELOPE) -> FrameDecoder:
    return FrameDecoder(envelope)


# ------------------------------------------------------------------------------------------
# The published frame format
# ------------------------------------------------------------------------------------------


def test_decodes_the_vendor_documented_example() -> None:
    """The worked example from the published protocol document, byte for byte.

    Three targets, alarm set, first target at 10 degrees and 40 metres receding at 60 km/h with
    a signal-to-noise of 21. If this ever fails, the field layout has been misread and every
    detection this driver produces is in the wrong place.
    """
    # Built explicitly rather than pasted, so the arithmetic behind the length is visible:
    # three targets at five bytes each, plus the count and alarm bytes, is 0x11.
    payload = bytes([0x03, 0x01]) + bytes(
        [
            0x8A, 0x28, 0x00, 0x3C, 0x15,
            0x8A, 0x30, 0x01, 0x1E, 0x40,
            0x76, 0x0A, 0x01, 0x05, 0x63,
        ]
    )
    raw = DATA_FRAME_HEADER + len(payload).to_bytes(2, "little") + payload + DATA_FRAME_TAIL

    assert len(payload) == 0x11

    frames = decoder().feed(raw)

    assert len(frames) == 1
    frame = frames[0]
    assert frame.alarm is True
    assert len(frame.targets) == 3

    first = frame.targets[0]
    assert first.angle_deg == 10, "angle is the byte minus 0x80"
    assert first.distance_m == 40
    assert first.approaching is False, "0x00 is receding"
    assert first.speed_kmh == 60
    assert first.snr == 21

    assert frame.targets[1].approaching is True
    assert frame.targets[2].angle_deg == -10, "angles below boresight are negative"


def test_a_frame_with_no_targets_is_valid_and_means_nothing_seen() -> None:
    """An empty sky produces empty frames, not silence, and they must decode.

    Rejecting them would make the link look intermittent every time nothing was in front of the
    sensor — and a decoder that only accepts frames with targets in them cannot tell a quiet
    sensor from a dead one, which is the distinction the whole system is built around.
    """
    frames = decoder().feed(encode_target_frame(TargetFrame(alarm=False, targets=())))

    assert len(frames) == 1
    assert frames[0].targets == ()


@pytest.mark.parametrize("count", [1, 2, 5, 10])
def test_round_trips_every_admissible_target_count(count: int) -> None:
    original = TargetFrame(
        alarm=True,
        targets=tuple(
            target(angle_deg=i - 5, distance_m=10 + i, approaching=True)
            for i in range(count)
        ),
    )

    frames = decoder().feed(encode_target_frame(original))

    assert frames == [original]


def test_boundary_values_of_every_field_are_accepted() -> None:
    """The envelope's edges are inside it. An off-by-one here would silently discard the
    furthest and fastest targets — exactly the ones that matter."""
    extreme = TargetFrame(
        alarm=True,
        targets=(
            target(
                angle_deg=ENVELOPE.max_azimuth_deg,
                distance_m=ENVELOPE.max_distance_m,
                approaching=True,
                speed_kmh=ENVELOPE.max_speed_kmh,
                snr=ENVELOPE.max_snr,
            ),
            target(
                angle_deg=-ENVELOPE.max_azimuth_deg,
                distance_m=ENVELOPE.min_distance_m,
                approaching=True,
                speed_kmh=0,
                snr=ENVELOPE.min_snr,
            ),
        ),
    )

    assert decoder().feed(encode_target_frame(extreme)) == [extreme]


# ------------------------------------------------------------------------------------------
# Streaming: frames do not arrive whole
# ------------------------------------------------------------------------------------------


def test_a_frame_split_across_reads_is_reassembled() -> None:
    """Serial reads land wherever they land. A decoder that needed whole frames would drop
    most of them under load and none of them on a quiet bench, which is the worst way for a
    bug like this to behave."""
    raw = encode_target_frame(TargetFrame(alarm=True, targets=(target(),)))
    engine = decoder()

    for index in range(len(raw) - 1):
        assert engine.feed(raw[index : index + 1]) == [], "completed early"

    frames = engine.feed(raw[-1:])
    assert len(frames) == 1


def test_a_header_split_across_reads_is_not_lost() -> None:
    """The buffer keeps the last three bytes when no header is found, because those three could
    be the start of one. Keeping fewer silently drops a frame per split header."""
    raw = encode_target_frame(TargetFrame(alarm=False, targets=(target(),)))
    engine = decoder()

    assert engine.feed(raw[:2]) == []
    assert len(engine.feed(raw[2:])) == 1


def test_several_frames_in_one_read_all_come_back() -> None:
    frames = [
        TargetFrame(alarm=False, targets=(target(distance_m=d),)) for d in (10, 20, 30)
    ]
    raw = b"".join(encode_target_frame(f) for f in frames)

    assert decoder().feed(raw) == frames


def test_the_buffer_stays_bounded_on_a_stream_of_garbage() -> None:
    """A decoder that accumulated unparseable input would exhaust memory on a wrong baud rate,
    which is a configuration mistake rather than an attack and must not take the process with
    it."""
    engine = decoder()
    noise = bytes(random.Random(7).randrange(256) for _ in range(20_000))

    engine.feed(noise)

    assert engine.pending_bytes < ENVELOPE.max_frame_length + len(DATA_FRAME_HEADER)


# ------------------------------------------------------------------------------------------
# Desynchronisation and structural corruption
# ------------------------------------------------------------------------------------------


def test_leading_noise_is_discarded_and_counted() -> None:
    """Every byte thrown away is accounted for. Silently discarding them would report a healthy
    sensor on a failing cable (ADR 0018)."""
    raw = b"\x00\x11\x22\x33" + encode_target_frame(
        TargetFrame(alarm=False, targets=(target(),))
    )
    engine = decoder()

    frames = engine.feed(raw)

    assert len(frames) == 1
    assert engine.statistics.bytes_discarded == 4
    assert engine.statistics.rejects[RejectReason.RESYNCHRONISED] == 4
    assert engine.statistics.byte_discard_rate > 0.0


def test_a_dropped_byte_costs_one_frame_and_the_stream_recovers() -> None:
    """The failure a marginal UART actually produces.

    Losing a byte shifts everything after it, so the frame it happened in is unreadable. What
    matters is that the *next* frame is not: a decoder that never resynchronised would go silent
    for the rest of the run after one glitch, and the sensor would appear to have died.
    """
    good = encode_target_frame(TargetFrame(alarm=False, targets=(target(),)))
    damaged = good[:8] + good[9:]

    engine = decoder()
    frames = engine.feed(damaged + good)

    assert len(frames) == 1, "the intact frame after the damaged one must still arrive"
    assert engine.statistics.frames_rejected >= 1


def test_a_corrupted_length_field_is_rejected_without_stalling() -> None:
    """A length that cannot describe a real frame must be refused immediately.

    Waiting for the bytes it promises is how a decoder hangs on corruption: 0xFFFF would mean
    waiting for 65 kilobytes that are never coming, and the sensor would appear silent for
    minutes.
    """
    raw = bytearray(encode_target_frame(TargetFrame(alarm=False, targets=(target(),))))
    raw[4:6] = (0xFF, 0xFF)

    engine = decoder()
    frames = engine.feed(bytes(raw) + encode_target_frame(TargetFrame(alarm=False, targets=())))

    assert len(frames) == 1, "the following frame must still decode"
    assert engine.statistics.rejects[RejectReason.IMPLAUSIBLE_LENGTH] >= 1


@pytest.mark.parametrize("length", [0, 1, 3, 4, 6, 8, 9])
def test_a_length_that_is_not_five_targets_plus_two_is_rejected(length: int) -> None:
    """`length == 5N + 2` is the equation that catches every length-changing corruption. Any
    other value cannot describe a frame this device produced."""
    raw = DATA_FRAME_HEADER + length.to_bytes(2, "little") + bytes(length) + DATA_FRAME_TAIL

    engine = decoder()
    engine.feed(raw)

    assert engine.statistics.frames_decoded == 0
    assert engine.statistics.rejects[RejectReason.IMPLAUSIBLE_LENGTH] >= 1


def test_a_tail_in_the_wrong_place_is_rejected() -> None:
    raw = bytearray(encode_target_frame(TargetFrame(alarm=False, targets=(target(),))))
    raw[-1] = 0x00

    engine = decoder()
    engine.feed(bytes(raw))

    assert engine.statistics.frames_decoded == 0
    assert engine.statistics.rejects[RejectReason.TAIL_MISMATCH] >= 1


def test_a_length_beyond_the_configured_maximum_is_rejected() -> None:
    """`max_targets` is what bounds the decoder's memory and its willingness to wait."""
    oversized = 5 * (ENVELOPE.max_targets + 1) + 2
    raw = DATA_FRAME_HEADER + oversized.to_bytes(2, "little") + bytes(oversized) + DATA_FRAME_TAIL

    engine = decoder()
    engine.feed(raw)

    assert engine.statistics.rejects[RejectReason.IMPLAUSIBLE_LENGTH] >= 1


# ------------------------------------------------------------------------------------------
# Value corruption — the failure the framing cannot catch
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field_index", "value", "why"),
    [
        (0, ANGLE_OFFSET + 90, "90 degrees is far outside a cone this device cannot see past"),
        (0, ANGLE_OFFSET - 90, "and the same on the other side of boresight"),
        (1, 200, "200 m is beyond the configured maximum range"),
        (2, 0x7F, "direction has exactly two defined values"),
        (3, 250, "250 km/h exceeds the configured speed ceiling"),
        (4, 200, "signal-to-noise is documented as 0 to 100"),
    ],
)
def test_an_implausible_target_value_is_rejected(field_index: int, value: int, why: str) -> None:
    """The envelope is the integrity check, so each bound has to actually hold.

    These are frames that are structurally perfect — correct header, correct length, correct
    tail — and wrong only in a value. Nothing in the protocol catches them; only this does.
    """
    raw = bytearray(encode_target_frame(TargetFrame(alarm=False, targets=(target(),))))
    payload_start = len(DATA_FRAME_HEADER) + 2 + 2
    raw[payload_start + field_index] = value

    engine = decoder()
    engine.feed(bytes(raw))

    assert engine.statistics.frames_decoded == 0, why
    assert engine.statistics.rejects[RejectReason.IMPLAUSIBLE_VALUE] >= 1


def test_a_count_disagreeing_with_the_length_is_rejected() -> None:
    """Two encodings of the same fact, so they check each other for free."""
    raw = bytearray(encode_target_frame(TargetFrame(alarm=False, targets=(target(), target()))))
    raw[6] = 5

    engine = decoder()
    engine.feed(bytes(raw))

    assert engine.statistics.rejects[RejectReason.IMPLAUSIBLE_VALUE] >= 1


def test_an_alarm_contradicting_every_target_direction_is_counted_but_not_rejected() -> None:
    """Deliberately a signal rather than a rejection.

    The alarm flag and the per-target directions look like two encodings of one fact, which
    would make disagreement a free corruption check. But the flag's semantics are documented
    only in passing and are unconfirmed against a physical unit, and rejecting on it would
    discard a frame full of individually plausible targets. Losing real detections is the one
    failure this system must not have, and it is strictly worse than admitting a corrupted flag
    nothing downstream reads.
    """
    contradictory = TargetFrame(
        alarm=True, targets=(target(approaching=False), target(approaching=False))
    )

    engine = decoder()
    frames = engine.feed(encode_target_frame(contradictory))

    assert frames == [contradictory], "the targets are plausible and must survive"
    assert engine.statistics.alarm_direction_disagreements == 1
    assert engine.statistics.frames_rejected == 0


def test_an_undefined_alarm_byte_is_rejected() -> None:
    raw = bytearray(encode_target_frame(TargetFrame(alarm=False, targets=(target(),))))
    raw[7] = 0x42

    engine = decoder()
    engine.feed(bytes(raw))

    assert engine.statistics.rejects[RejectReason.IMPLAUSIBLE_VALUE] >= 1


def test_a_rejected_frame_does_not_swallow_the_next_one() -> None:
    """When the framing held, the frame's extent is known even though its contents are not, so
    the decoder skips exactly that far. Rescanning inside a frame already known to be bad would
    find noise and could consume the frame after it."""
    bad = bytearray(encode_target_frame(TargetFrame(alarm=False, targets=(target(),))))
    bad[len(DATA_FRAME_HEADER) + 2 + 2 + 1] = 200

    good = TargetFrame(alarm=False, targets=(target(distance_m=55),))

    engine = decoder()
    frames = engine.feed(bytes(bad) + encode_target_frame(good))

    assert frames == [good]


# ------------------------------------------------------------------------------------------
# The measured residual — ADR 0018
# ------------------------------------------------------------------------------------------


def test_the_declared_residual_matches_what_corruption_actually_achieves() -> None:
    """The number ADR 0018 declares is checked against reality, not asserted.

    Every single-byte corruption of a three-target frame is enumerated — every byte position,
    every one of the 255 alternative values — and the fraction the decoder accepts is compared
    against the computed residual. If the arithmetic and the checks ever disagree, the declared
    integrity of this link is wrong, and it is the kind of wrong nobody would notice.
    """
    frame = TargetFrame(
        alarm=True,
        targets=(
            target(approaching=True),
            target(angle_deg=-10, distance_m=48, approaching=True, speed_kmh=30, snr=64),
            target(angle_deg=0, distance_m=10, approaching=True, speed_kmh=5, snr=99),
        ),
    )
    raw = encode_target_frame(frame)

    accepted = 0
    trials = 0
    for index in range(len(raw)):
        for value in range(256):
            if value == raw[index]:
                continue
            trials += 1
            mutated = bytearray(raw)
            mutated[index] = value

            engine = decoder()
            decoded = engine.feed(bytes(mutated))
            # Accepted *and different from the truth* is what undetected corruption means. A
            # mutation the decoder rejects is caught; one that round-trips to the original frame
            # cannot happen, since the value differs by construction.
            if decoded and decoded != [frame]:
                accepted += 1

    measured = accepted / trials
    predicted = residual_corruption_probability(ENVELOPE, targets=len(frame.targets))

    assert measured == pytest.approx(predicted, abs=0.02), (
        f"the decoder lets {measured:.1%} of single-byte corruptions through, but the declared "
        f"residual is {predicted:.1%}. One of the two is wrong, and a declared integrity figure "
        "that does not describe the decoder is worse than none"
    )


def test_the_declared_residual_is_the_worst_case_over_frame_size() -> None:
    """Declared at the maximum target count, because the fully-checked delimiter bytes are a
    fixed cost per frame: the more targets a frame carries, the smaller their protective share
    of it. A safety-relevant figure must err towards pessimism."""
    by_size = [
        residual_corruption_probability(ENVELOPE, targets=n)
        for n in range(1, ENVELOPE.max_targets + 1)
    ]

    assert by_size == sorted(by_size), "residual must rise with frame size"
    assert residual_corruption_probability(ENVELOPE) == pytest.approx(by_size[-1])


def test_tightening_the_envelope_measurably_improves_integrity() -> None:
    """The operator's only lever on a link with no checksum, and the reason every bound is
    configuration rather than a constant in the decoder."""
    loose = residual_corruption_probability(ENVELOPE, targets=3)
    tight = residual_corruption_probability(TIGHT_ENVELOPE, targets=3)

    assert tight < loose
    assert loose - tight > 0.04, "the speed ceiling should be worth a real reduction"


def test_a_speed_ceiling_that_was_tightened_actually_rejects_more() -> None:
    """The residual is arithmetic; this is the behaviour behind it."""
    fast = TargetFrame(alarm=False, targets=(target(speed_kmh=150),))
    raw = encode_target_frame(fast)

    assert decoder(ENVELOPE).feed(raw) == [fast]
    assert decoder(TIGHT_ENVELOPE).feed(raw) == []


def test_the_estimated_undetected_rate_rises_with_the_observed_reject_rate() -> None:
    """The step that turns counting into a conclusion: undetected corruption is unobservable,
    but it shares a cause with the corruption that is observable, so one estimates the other."""
    engine = decoder()
    good = encode_target_frame(TargetFrame(alarm=False, targets=(target(),)))

    engine.feed(good * 10)
    clean = engine.statistics.estimated_undetected_rate(engine.residual_corruption_probability)

    bad = bytearray(good)
    bad[len(DATA_FRAME_HEADER) + 2 + 2 + 1] = 250
    engine.feed(bytes(bad) * 10)
    dirty = engine.statistics.estimated_undetected_rate(engine.residual_corruption_probability)

    assert clean == 0.0
    assert dirty > clean


# ------------------------------------------------------------------------------------------
# Configuration acknowledgements share the wire
# ------------------------------------------------------------------------------------------


def test_a_command_acknowledgement_is_skipped_rather_than_counted_as_corruption() -> None:
    """The module answers configuration commands on the same UART. Treating those replies as
    desynchronisation would inflate the very rate that is supposed to mean the cable is
    failing."""
    ack_payload = bytes([0x02, 0x01, 0x00, 0x00])
    ack = (
        COMMAND_FRAME_HEADER
        + len(ack_payload).to_bytes(2, "little")
        + ack_payload
        + COMMAND_FRAME_TAIL
    )
    report = TargetFrame(alarm=False, targets=(target(),))

    engine = decoder()
    frames = engine.feed(ack + encode_target_frame(report))

    assert frames == [report]
    assert engine.statistics.command_frames_skipped == 1
    assert engine.statistics.rejects[RejectReason.RESYNCHRONISED] == 0
    assert engine.statistics.frames_rejected == 0


# ------------------------------------------------------------------------------------------
# Assumed altitude — ADR 0019
# ------------------------------------------------------------------------------------------


def test_assumed_altitude_uncertainty_grows_with_range() -> None:
    """A device with no elevation channel knows less about altitude the further out it looks,
    and its reported covariance has to say so or the filter will believe the assumption."""
    near = assumed_altitude_sigma_m(50.0, vertical_aperture_deg=30.0)
    far = assumed_altitude_sigma_m(100.0, vertical_aperture_deg=30.0)

    assert far == pytest.approx(2.0 * near)
    assert far == pytest.approx(100.0 * 0.2679 / 1.7320, rel=1e-3)


def test_assumed_altitude_uncertainty_is_zero_at_the_sensor() -> None:
    """Degenerate but correct: at zero range the beam has no vertical extent, so there is
    nothing to be uncertain about."""
    assert assumed_altitude_sigma_m(0.0, vertical_aperture_deg=30.0) == 0.0


def test_a_negative_range_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        assumed_altitude_sigma_m(-1.0, vertical_aperture_deg=30.0)


# ------------------------------------------------------------------------------------------
# Envelope validation
# ------------------------------------------------------------------------------------------


def test_an_envelope_with_inverted_bounds_is_refused() -> None:
    with pytest.raises(ValueError, match="min_distance_m must be below max_distance_m"):
        ENVELOPE.model_copy(update={"min_distance_m": 200}).model_validate(
            {**ENVELOPE.model_dump(), "min_distance_m": 200}
        )


def test_an_envelope_rejects_unknown_fields() -> None:
    """`extra="forbid"` everywhere: a misspelled bound must not silently leave the real one at
    its permissive default, because the default is the loosest integrity setting available."""
    with pytest.raises(ValueError):
        PlausibilityEnvelope(**{**ENVELOPE.model_dump(), "max_speed_kph": 80})
