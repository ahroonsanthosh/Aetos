"""Identifier generation for detections, tracks and events.

Identifiers are **deterministic and sequential**, not random. A scenario replayed from the
same seed must produce the same identifiers, otherwise replay output cannot be compared
against the original run and the replay feature loses most of its debugging value.

Sequence state is held per-generator rather than globally, so two scenarios running in the
same process do not interleave their counters. See docs/adr/0008 on determinism.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterator

_DETECTION_PREFIX = "det"
_TRACK_PREFIX = "TRK"
_EVENT_PREFIX = "evt"

_TRACK_ID_PATTERN = re.compile(r"^TRK-\d{5,}$")


class IdGenerator:
    """Monotonic identifier source for one namespace.

    Not thread-safe by design: identifier generation sits behind the same single-writer
    boundary as the event log (ADR 0006), so adding a lock here would signal a concurrency
    model we deliberately do not have.
    """

    __slots__ = ("_prefix", "_width", "_counter", "_scope")

    def __init__(self, prefix: str, width: int = 6, start: int = 1, scope: str = "") -> None:
        self._prefix = prefix
        self._width = width
        self._scope = scope
        self._counter: Iterator[int] = itertools.count(start)

    def next(self) -> str:
        n = next(self._counter)
        body = f"{n:0{self._width}d}"
        if self._scope:
            return f"{self._prefix}-{self._scope}-{body}"
        return f"{self._prefix}-{body}"


def detection_id_generator(sensor_id: str) -> IdGenerator:
    """Detection IDs scoped to a sensor.

    Scoping by sensor means each simulated sensor numbers its own detections independently,
    so adding a sensor to a scenario does not shift the identifiers produced by the others.
    That keeps scenario diffs readable when sensors are added during development.
    """
    return IdGenerator(_DETECTION_PREFIX, width=6, scope=_sanitise_scope(sensor_id))


def track_id_generator() -> IdGenerator:
    """Track IDs. Global to a tracking session — a track is not owned by any one sensor."""
    return IdGenerator(_TRACK_PREFIX, width=5)


def event_id_generator() -> IdGenerator:
    """Event IDs.

    Note that the event log's ordering authority is its monotonic `seq`, assigned by the
    single writer, not this identifier. The event ID is a stable human-readable handle; `seq`
    is the total order. See docs/adr/0006.
    """
    return IdGenerator(_EVENT_PREFIX, width=8)


def is_valid_track_id(value: str) -> bool:
    """Whether a string is a well-formed track identifier."""
    return bool(_TRACK_ID_PATTERN.match(value))


def _sanitise_scope(value: str) -> str:
    """Reduce an arbitrary sensor ID to something safe inside an identifier.

    Sensor IDs come from configuration and, in future, from real hardware, so they are not
    trusted to be free of separators that would make the resulting identifier ambiguous.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", value)
    if not cleaned:
        raise ValueError(f"sensor_id {value!r} contains no usable characters for an identifier")
    return cleaned.lower()
