"""The link-quality window and the transport seam.

The window is the mechanism behind ADR 0018's last step: undetected corruption cannot be
observed, but it shares a cause with the corruption that can be, so the observable rate is used
to degrade the sensor. What matters is that the window is a *window* — a link that was clean for
an hour and has been failing for ten seconds must read as failing, and a cumulative average
would read as clean.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from sensors.adapters.ld2451.link import LinkQualityWindow
from sensors.adapters.ld2451.protocol import DecoderStatistics, RejectReason
from sensors.adapters.ld2451.transport import (
    FakeTransport,
    SerialTransport,
    Transport,
    TransportError,
)

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def statistics(*, decoded: int = 0, rejected: int = 0, discarded: int = 0) -> DecoderStatistics:
    totals = DecoderStatistics(frames_decoded=decoded, bytes_discarded=discarded)
    if rejected:
        totals.rejects[RejectReason.TAIL_MISMATCH] = rejected
    return totals


# ------------------------------------------------------------------------------------------
# The window
# ------------------------------------------------------------------------------------------


def test_an_empty_window_is_silent_rather_than_healthy() -> None:
    """Zero rejects out of zero frames is not a clean link, and reading it as one would report a
    dead sensor as nominal. The raw counts are carried alongside the rate so the caller can
    tell."""
    window = LinkQualityWindow(SimClock(EPOCH), window_s=10.0)

    quality = window.quality()

    assert quality.silent
    assert quality.frame_reject_rate == 0.0
    assert quality.frames_decoded == 0


def test_rates_are_computed_from_differences_between_samples() -> None:
    clock = SimClock(EPOCH)
    window = LinkQualityWindow(clock, window_s=10.0)

    window.observe(statistics(decoded=0, rejected=0, discarded=0), bytes_seen_total=0)
    clock.advance(1.0)
    window.observe(statistics(decoded=8, rejected=2, discarded=40), bytes_seen_total=1000)

    quality = window.quality()

    assert quality.frames_decoded == 8
    assert quality.frames_rejected == 2
    assert quality.frame_reject_rate == pytest.approx(0.2)
    assert quality.byte_discard_rate == pytest.approx(0.04)
    assert not quality.silent


def test_the_window_forgets_a_link_that_has_since_recovered() -> None:
    """The property the whole thing exists for. A cumulative average would still be reporting
    the bad ten seconds an hour later, and an operator would stop believing the health record."""
    clock = SimClock(EPOCH)
    window = LinkQualityWindow(clock, window_s=5.0)

    window.observe(statistics(), bytes_seen_total=0)
    clock.advance(1.0)
    window.observe(statistics(decoded=1, rejected=9, discarded=500), bytes_seen_total=1000)
    assert window.quality().frame_reject_rate == pytest.approx(0.9)

    clock.advance(10.0)
    window.observe(statistics(decoded=101, rejected=9, discarded=500), bytes_seen_total=11000)

    recovered = window.quality()
    assert recovered.frames_rejected == 0
    assert recovered.frame_reject_rate == 0.0


def test_the_window_notices_a_link_that_has_just_started_failing() -> None:
    """The mirror of the previous test, and the one that matters operationally."""
    clock = SimClock(EPOCH)
    window = LinkQualityWindow(clock, window_s=5.0)

    window.observe(statistics(), bytes_seen_total=0)
    for step in range(1, 11):
        clock.advance(1.0)
        window.observe(statistics(decoded=100 * step, discarded=0), bytes_seen_total=10_000 * step)
    assert window.quality().frame_reject_rate == 0.0

    clock.advance(1.0)
    window.observe(
        statistics(decoded=1010, rejected=90, discarded=4000), bytes_seen_total=115_000
    )

    # The window still holds four seconds of clean history, so the rate is diluted rather than
    # spiking to the failing second's own 90%. That is the intended trade: a window short enough
    # to react but long enough that one bad frame is not a fault. What matters is that it clears
    # the degraded threshold immediately, which it does by an order of magnitude.
    degraded = window.quality().frame_reject_rate
    assert degraded == pytest.approx(90 / 600)
    assert degraded > 0.02

    # And once the clean history ages out, it reads as badly as the link actually is.
    clock.advance(4.0)
    window.observe(
        statistics(decoded=1015, rejected=180, discarded=8000), bytes_seen_total=125_000
    )
    assert window.quality().frame_reject_rate > 0.5


def test_the_estimated_undetected_rate_scales_with_the_residual() -> None:
    """ADR 0018's final step. A link with a tighter plausibility envelope leaks less through the
    same observed reject rate, which is why tightening the envelope is an integrity control."""
    clock = SimClock(EPOCH)
    window = LinkQualityWindow(clock, window_s=10.0)
    window.observe(statistics(), bytes_seen_total=0)
    clock.advance(1.0)
    window.observe(statistics(decoded=90, rejected=10), bytes_seen_total=5000)

    quality = window.quality()

    assert quality.estimated_undetected_rate(0.0) == 0.0
    loose = quality.estimated_undetected_rate(0.28)
    tight = quality.estimated_undetected_rate(0.14)
    assert tight < loose
    assert loose == pytest.approx(0.1 * 0.28 / 0.72)


def test_a_non_positive_window_is_refused() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        LinkQualityWindow(SimClock(EPOCH), window_s=0.0)


# ------------------------------------------------------------------------------------------
# The transport seam
# ------------------------------------------------------------------------------------------


def test_the_fake_satisfies_the_protocol() -> None:
    """A `Protocol` rather than a base class, so a double is any object with the right shape.
    Asserted so the seam cannot drift from what implements it."""
    assert isinstance(FakeTransport(), Transport)
    assert isinstance(SerialTransport("/dev/null", 115200), Transport)


def test_reading_an_unopened_link_raises_rather_than_returning_nothing() -> None:
    """An empty read means a quiet sensor. A closed port returning one would be a wiring error
    disguised as an empty sky, which is the failure the whole lifecycle contract is about."""
    transport = FakeTransport([b"\x01"])

    with pytest.raises(TransportError):
        transport.read(16, 0.1)


def test_open_and_close_are_idempotent() -> None:
    transport = FakeTransport([b"\x01"])

    transport.open()
    transport.open()
    assert transport.open_count == 1

    transport.close()
    transport.close()
    assert transport.close_count == 1
    assert not transport.is_open


def test_an_exhausted_script_reads_as_quiet_rather_than_broken() -> None:
    """Which is what a real port does when the sensor has nothing to say, and the state the
    silence timeout is judged on."""
    transport = FakeTransport([b"\x01\x02"])
    transport.open()

    assert transport.read(16, 0.1) == b"\x01\x02"
    assert transport.read(16, 0.1) == b""
    assert transport.read(16, 0.1) == b""


def test_a_scripted_failure_surfaces_as_a_transport_error() -> None:
    transport = FakeTransport([b"\x01"] * 10, fail_after=2)
    transport.open()

    transport.read(16, 0.1)
    transport.read(16, 0.1)
    with pytest.raises(TransportError, match="scripted link failure"):
        transport.read(16, 0.1)


def test_opening_a_port_that_does_not_exist_says_what_to_check() -> None:
    """The most common first failure with new hardware, and one where the operating system's own
    message says nothing about permissions or which name would have worked."""
    transport = SerialTransport("/dev/definitely-not-a-serial-port", 115200)

    with pytest.raises(TransportError, match="dialout"):
        transport.open()


def test_closing_a_link_that_never_opened_is_safe() -> None:
    SerialTransport("/dev/definitely-not-a-serial-port", 115200).close()
