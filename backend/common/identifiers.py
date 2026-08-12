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
_ALERT_PREFIX = "ALT"

_TRACK_ID_PATTERN = re.compile(r"^TRK-(\d{5,})$")
_ALERT_ID_PATTERN = re.compile(r"^ALT-(\d{5,})$")
_EVENT_ID_PATTERN = re.compile(r"^evt-(\d{8,})$")


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


def track_id_generator(start: int = 1) -> IdGenerator:
    """Track IDs. Global to a tracking session — a track is not owned by any one sensor.

    `start` exists for the same reason as `alert_id_generator`'s: track identifiers must not be
    reused across a restart. Tracks are retained forever in `track_history`, keyed by
    `(track_id, seq)`, so a second TRK-00001 does not overwrite the first — it *appends to it*,
    and the history of two physically different objects becomes one indistinguishable record.
    That is a corrupted audit trail rather than a merely incomplete one, which is why the
    resume point comes from the store rather than from a counter. See `track_id_sequence`.
    """
    return IdGenerator(_TRACK_PREFIX, width=5, start=start)


def event_id_generator(start: int = 1) -> IdGenerator:
    """Event IDs.

    Note that the event log's ordering authority is its monotonic `seq`, assigned by the
    single writer, not this identifier. The event ID is a stable human-readable handle; `seq`
    is the total order. See docs/adr/0006.

    `start` resumes the sequence after a restart. It must be derived from the highest
    identifier the log actually contains, never from how many rows it has: an append that fails
    after the identifier is drawn but before the row lands leaves a permanent gap, after which
    the row count is one short of the high-water mark for every subsequent append. Counting
    would then re-issue an identifier that is already stored, and `event_id` is UNIQUE — so the
    log would refuse the write, turning one transient failure into a permanently broken store.
    See `event_id_sequence`.
    """
    return IdGenerator(_EVENT_PREFIX, width=8, start=start)


def alert_id_generator(start: int = 1) -> IdGenerator:
    """Alert IDs.

    `start` exists because alert identifiers must not be reused across a restart. Alerts are
    rebuilt by replaying the log, so the generator has to resume above the highest identifier
    the log already contains — a second `ALT-00007` would make two distinct alerts
    indistinguishable in the record, and an acknowledgement of one would appear to
    acknowledge the other. See `alert_id_sequence`.
    """
    return IdGenerator(_ALERT_PREFIX, width=5, start=start)


def alert_id_sequence(value: str) -> int | None:
    """The counter inside an alert identifier, or None if it is not one.

    Used when resuming the generator after replay. Returning None rather than raising lets a
    caller scan a log that may contain identifiers written by an older format without having
    to guard every entry.
    """
    match = _ALERT_ID_PATTERN.match(value)
    return int(match.group(1)) if match else None


def track_id_sequence(value: str) -> int | None:
    """The counter inside a track identifier, or None if it is not one.

    The counterpart to `alert_id_sequence`, used to resume the track generator above whatever
    the store already holds. None for anything unrecognised, so a store containing identifiers
    from an older format is skipped rather than crashing the resume.
    """
    match = _TRACK_ID_PATTERN.match(value)
    return int(match.group(1)) if match else None


def event_id_sequence(value: str) -> int | None:
    """The counter inside an event identifier, or None if it is not one."""
    match = _EVENT_ID_PATTERN.match(value)
    return int(match.group(1)) if match else None


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
