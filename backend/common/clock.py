"""Injectable clocks. The only place in backend/ or sensors/ that reads real time.

Every module takes a Clock at construction rather than calling `datetime.now()`. This is what
makes replay, deterministic tests and simulated clock skew possible at all — none of them can
be retrofitted once wall-clock calls are scattered through a dozen modules.

Enforced by tests/unit/test_module_boundaries.py. See docs/adr/0003.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta


class Clock(ABC):
    """Source of time.

    Two distinct notions, deliberately not interchangeable:

    - `now()` is wall time, used for timestamps that leave the system. It can jump backwards
      under NTP correction.
    - `monotonic()` is elapsed time, used for intervals, timeouts and staleness. It never
      moves backwards.

    Using wall time for timeout logic produces bugs that appear only during clock correction
    and are close to undiagnosable, so the two are kept separate at the interface.
    """

    @abstractmethod
    def now(self) -> datetime:
        """Current wall time, timezone-aware, UTC."""

    @abstractmethod
    def monotonic(self) -> float:
        """Seconds elapsed from an arbitrary fixed origin. Never decreases."""


class SystemClock(Clock):
    """Real time. Used in live operation."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class SimClock(Clock):
    """Manually advanced time. Used by scenarios, replay and most tests.

    Time only moves when something advances it, which is what makes tests deterministic: a
    track going stale after thirty seconds is tested by advancing thirty seconds, not by
    sleeping for them.

    `rate` scales advancement for replay transport control: 0.0 pauses, 0.5 is half speed,
    4.0 is four times speed. It applies to `advance_real_elapsed`, which is how a replay
    driver converts real elapsed time into simulated time. Direct `advance()` calls are
    unaffected, since a test asking for exactly thirty seconds means exactly thirty seconds.
    """

    def __init__(self, start: datetime, rate: float = 1.0) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock start must be timezone-aware")
        self._now = start.astimezone(UTC)
        self._monotonic = 0.0
        self._rate = rate

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate: float) -> None:
        """Set the replay rate. 0.0 pauses; values above 1.0 fast-forward."""
        if rate < 0.0:
            raise ValueError("rate must be non-negative; time does not run backwards")
        self._rate = rate

    def advance(self, seconds: float) -> None:
        """Advance simulated time by exactly `seconds`, ignoring `rate`."""
        if seconds < 0.0:
            raise ValueError("cannot advance a clock backwards")
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds

    def advance_real_elapsed(self, real_seconds: float) -> None:
        """Advance by `real_seconds` scaled by `rate`. Used by the replay driver."""
        self.advance(real_seconds * self._rate)

    def advance_to(self, target: datetime) -> None:
        """Advance to a specific wall time. Raises if that would move time backwards."""
        if target.tzinfo is None:
            raise ValueError("target must be timezone-aware")
        target = target.astimezone(UTC)
        delta = (target - self._now).total_seconds()
        if delta < 0.0:
            raise ValueError("cannot advance a clock backwards")
        self.advance(delta)


class OffsetClock(Clock):
    """Another clock with a fixed wall-time offset.

    Used to give a simulated sensor a clock that disagrees with the rest of the system, so
    that clock-skew handling is a tested behaviour rather than an assumption. See
    docs/failure_modes.md FM-012.

    The offset applies to `now()` only. `monotonic()` passes through unchanged, because skew
    between systems is a wall-clock phenomenon — elapsed time still elapses at the same rate.
    """

    def __init__(self, base: Clock, offset_seconds: float) -> None:
        self._base = base
        self._offset = timedelta(seconds=offset_seconds)

    def now(self) -> datetime:
        return self._base.now() + self._offset

    def monotonic(self) -> float:
        return self._base.monotonic()
