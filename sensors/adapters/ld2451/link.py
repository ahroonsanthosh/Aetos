"""Judging a link that cannot be trusted, from the evidence it does provide.

ADR 0018's argument in one paragraph: undetected payload corruption on a checksumless protocol
is unobservable by definition, but it shares a physical cause with the corruption that *is*
observable — a failing cable, a wrong baud rate, interference, a buffer overrun. So the
detected rate estimates the undetected one, and a sensor can be degraded on evidence rather
than on optimism.

`DecoderStatistics` counts cumulatively, which answers "how has this run gone". That is the
wrong question for a health record: a link that was perfect for an hour and has been failing for
the last ten seconds still reads as healthy on a cumulative average, and the ten seconds are the
part an operator needs. This holds a sliding window instead.

Separate from the adapter so it can be tested on its own, against a clock that is stepped rather
than waited on.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from backend.common.clock import Clock
from sensors.adapters.ld2451.protocol import DecoderStatistics


@dataclass(frozen=True, slots=True)
class _Sample:
    """What changed between two observations of the decoder's counters."""

    at_monotonic: float
    frames_decoded: int
    frames_rejected: int
    bytes_seen: int
    bytes_discarded: int


@dataclass(frozen=True, slots=True)
class LinkQuality:
    """What the window says about the link right now."""

    frames_decoded: int
    frames_rejected: int
    bytes_seen: int
    bytes_discarded: int
    window_s: float

    @property
    def frame_reject_rate(self) -> float:
        """Rejected candidates as a fraction of all candidates in the window.

        Zero when nothing arrived, which is **not** the same as a healthy link — it is the
        signature of a dead one. The adapter distinguishes the two by `bytes_seen`, which is why
        the raw counts are carried here alongside the rate rather than being reduced to it.
        """
        candidates = self.frames_decoded + self.frames_rejected
        return self.frames_rejected / candidates if candidates else 0.0

    @property
    def byte_discard_rate(self) -> float:
        """Bytes thrown away resynchronising, as a fraction of those that arrived.

        The most sensitive indicator available. A framing error shifts the whole stream, so a
        marginal line shows up here well before it produces a frame that fails a value check.
        """
        return self.bytes_discarded / self.bytes_seen if self.bytes_seen else 0.0

    @property
    def silent(self) -> bool:
        return self.bytes_seen == 0

    def estimated_undetected_rate(self, residual: float) -> float:
        """Frames per candidate that are probably corrupt and were accepted anyway.

        The step that turns counting into a conclusion. See
        `DecoderStatistics.estimated_undetected_rate` for the derivation and its assumptions;
        this applies it to the window rather than to the whole run.
        """
        if residual <= 0.0:
            return 0.0
        if residual >= 1.0:
            return float("inf")
        return self.frame_reject_rate * residual / (1.0 - residual)


class LinkQualityWindow:
    """A sliding view of the decoder's counters.

    Fed by sampling cumulative totals rather than by being told about individual events: the
    decoder already counts, and a second event stream would be a second thing to keep in step
    with it. Differences between consecutive samples are what the window holds.
    """

    def __init__(self, clock: Clock, window_s: float) -> None:
        if window_s <= 0.0:
            raise ValueError(f"window_s must be positive, got {window_s}")
        self._clock = clock
        self._window_s = window_s
        self._samples: deque[_Sample] = deque()
        self._previous: _Sample | None = None

    @property
    def window_s(self) -> float:
        return self._window_s

    def observe(self, statistics: DecoderStatistics, bytes_seen_total: int) -> None:
        """Record the decoder's totals as of now.

        `bytes_seen_total` comes from the adapter rather than the decoder: the decoder is told
        about bytes it is given, and the adapter is the one that knows how many arrived,
        including any it never passed on.
        """
        now = self._clock.monotonic()
        current = _Sample(
            at_monotonic=now,
            frames_decoded=statistics.frames_decoded,
            frames_rejected=statistics.frames_rejected,
            bytes_seen=bytes_seen_total,
            bytes_discarded=statistics.bytes_discarded,
        )

        if self._previous is not None:
            self._samples.append(
                _Sample(
                    at_monotonic=now,
                    frames_decoded=current.frames_decoded - self._previous.frames_decoded,
                    frames_rejected=current.frames_rejected - self._previous.frames_rejected,
                    bytes_seen=current.bytes_seen - self._previous.bytes_seen,
                    bytes_discarded=current.bytes_discarded - self._previous.bytes_discarded,
                )
            )

        self._previous = current
        self._prune(now)

    def quality(self) -> LinkQuality:
        """The window as it stands. Prunes first, so a link that went quiet reports as quiet
        rather than reporting whatever it was doing when it stopped."""
        self._prune(self._clock.monotonic())
        return LinkQuality(
            frames_decoded=sum(s.frames_decoded for s in self._samples),
            frames_rejected=sum(s.frames_rejected for s in self._samples),
            bytes_seen=sum(s.bytes_seen for s in self._samples),
            bytes_discarded=sum(s.bytes_discarded for s in self._samples),
            window_s=self._window_s,
        )

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._samples and self._samples[0].at_monotonic < cutoff:
            self._samples.popleft()
