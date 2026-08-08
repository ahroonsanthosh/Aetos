"""Clock behaviour: determinism, monotonicity, replay rate and simulated skew."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.common.clock import OffsetClock, SimClock, SystemClock

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def test_system_clock_returns_aware_utc() -> None:
    clock = SystemClock()
    now = clock.now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_system_clock_monotonic_never_decreases() -> None:
    clock = SystemClock()
    readings = [clock.monotonic() for _ in range(50)]

    assert readings == sorted(readings)


def test_sim_clock_only_moves_when_advanced() -> None:
    """The property that makes tests deterministic: nothing moves unless asked."""
    clock = SimClock(START)

    assert clock.now() == START
    assert clock.now() == START

    clock.advance(30.0)
    assert clock.now() == START + timedelta(seconds=30)
    assert clock.monotonic() == pytest.approx(30.0)


def test_sim_clock_rejects_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SimClock(datetime(2026, 8, 8, 12, 0, 0))


def test_sim_clock_refuses_to_run_backwards() -> None:
    clock = SimClock(START)

    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1.0)

    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(START - timedelta(seconds=1))


def test_sim_clock_advance_to_target() -> None:
    clock = SimClock(START)
    target = START + timedelta(minutes=5)

    clock.advance_to(target)

    assert clock.now() == target
    assert clock.monotonic() == pytest.approx(300.0)


@pytest.mark.parametrize(
    ("rate", "real_seconds", "expected_sim_seconds"),
    [
        (0.0, 10.0, 0.0),  # paused
        (0.25, 10.0, 2.5),  # slow motion
        (1.0, 10.0, 10.0),  # real time
        (8.0, 10.0, 80.0),  # fast forward
    ],
)
def test_replay_rate_scales_elapsed_time(
    rate: float, real_seconds: float, expected_sim_seconds: float
) -> None:
    """Replay transport control. Rate 0 is pause, which must genuinely stop time."""
    clock = SimClock(START, rate=rate)

    clock.advance_real_elapsed(real_seconds)

    assert clock.monotonic() == pytest.approx(expected_sim_seconds)


def test_direct_advance_ignores_rate() -> None:
    """A test asking for thirty seconds means thirty seconds regardless of replay rate."""
    clock = SimClock(START, rate=4.0)

    clock.advance(30.0)

    assert clock.monotonic() == pytest.approx(30.0)


def test_rate_cannot_be_negative() -> None:
    clock = SimClock(START)

    with pytest.raises(ValueError, match="non-negative"):
        clock.set_rate(-1.0)


def test_offset_clock_skews_wall_time_but_not_elapsed_time() -> None:
    """Clock skew between sensors is a wall-clock phenomenon. Elapsed time still elapses at
    the same rate on both machines, so monotonic must pass through unchanged."""
    base = SimClock(START)
    skewed = OffsetClock(base, offset_seconds=2.5)

    assert skewed.now() == START + timedelta(seconds=2.5)
    assert skewed.monotonic() == base.monotonic()

    base.advance(10.0)

    assert skewed.now() == START + timedelta(seconds=12.5)
    assert skewed.monotonic() == pytest.approx(base.monotonic())


def test_offset_clock_supports_negative_skew() -> None:
    base = SimClock(START)
    behind = OffsetClock(base, offset_seconds=-3.0)

    assert behind.now() == START - timedelta(seconds=3)
