"""Whether the pipeline is actually running, as opposed to merely being up.

FM-032 asks the event store to "fail loudly and visibly". The loud half was already true — a
cycle that raises is logged with a traceback and a running count. The visible half was not: the
count lived in a log line, and every read surface an operator or a monitoring probe can reach
reported the system as healthy while the picture silently stopped advancing.

That gap is the specific shape of the worst failure this system can have. The HTTP server, the
WebSocket and the projection all keep working when the pipeline stops, because none of them
depend on it having run. The operator sees a console that loads, a track list that answers, and
a map that draws — all of it frozen at the last cycle that succeeded, with nothing anywhere
saying so. A blank screen would be safer, because a blank screen is obviously wrong.

This module holds the smallest thing that closes it: a record of how the last cycle went, wired
to the health endpoint. It is deliberately *not* the watchdog. Sensor-silence detection and the
degraded-mode controller (FM-010, spec §16) are a larger piece of work and are still unbuilt;
what is here reports only on the pipeline's own liveness, and says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

DEGRADED_AFTER_FAILURES = 1
"""Consecutive failures before the pipeline is no longer reported as healthy.

One. Not a tolerance band: a cycle that raised did not produce a picture, and the operator is
looking at a stale one from that moment onward, not from the third occurrence. The distinction
between one failure and many is carried in the count rather than in whether anything is said.
"""

UNAVAILABLE_AFTER_FAILURES = 5
"""Consecutive failures past which this is an outage rather than a bad cycle.

A single failure can be a transient lock or a momentarily full disk that the next tick clears.
Five consecutive means nothing is clearing it, and the difference matters to whoever is paged:
one is worth noticing, the other is worth waking up for.
"""


@dataclass(slots=True)
class CycleHealth:
    """How the pipeline's most recent cycles went.

    Written only by the loop that drives the pipeline, read only by the health endpoint. Held
    in memory and deliberately not persisted: it describes *this* process's liveness, and a
    value restored from disk would describe the health of a process that is no longer running.
    """

    completed: int = 0
    failed: int = 0
    consecutive_failures: int = 0
    last_completed_at: datetime | None = None
    last_failed_at: datetime | None = None
    last_error: str | None = None

    def record_success(self, at: datetime) -> None:
        self.completed += 1
        self.consecutive_failures = 0
        self.last_completed_at = at

    def record_failure(self, at: datetime, error: BaseException) -> None:
        self.failed += 1
        self.consecutive_failures += 1
        self.last_failed_at = at
        # The type as well as the message. "disk I/O error" and "database is locked" arrive as
        # the same exception class from very different causes, and the reverse is also true.
        self.last_error = f"{type(error).__name__}: {error}"

    @property
    def is_healthy(self) -> bool:
        return self.consecutive_failures < DEGRADED_AFTER_FAILURES

    @property
    def is_outage(self) -> bool:
        return self.consecutive_failures >= UNAVAILABLE_AFTER_FAILURES

    def describe(self) -> str:
        """A sentence for the health endpoint, written for whoever is reading it at 3am."""
        if self.is_healthy:
            if self.completed == 0:
                return "The pipeline has not yet completed a cycle."
            return (
                f"Cycles are completing. {self.completed} completed, {self.failed} failed since "
                "this process started."
            )

        return (
            f"The pipeline has failed {self.consecutive_failures} consecutive cycle(s) and the "
            f"operator picture is NOT advancing. Last error — {self.last_error}"
        )
