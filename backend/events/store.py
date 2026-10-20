"""Append-only durable event store.

The store is in the pipeline path, not beside it. Ingestion appends; tracking, the WebSocket
broadcaster and the replay engine all read back by sequence number. That is what makes replay
trustworthy — it is the same read path at a different rate rather than a second
implementation that drifts from live behaviour.

Four properties, all load-bearing (docs/adr/0006):

1. Single writer. There is one append path and no concurrent one.
2. Monotonic `seq`, assigned by that writer. The total order of everything that happened.
3. Write-before-read. An event is durably committed before any consumer is notified.
4. Immutable. No update, no delete. Corrections are new events.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from backend.common.clock import Clock
from backend.common.identifiers import event_id_generator
from backend.events.types import Event, EventType

Subscriber = Callable[[Event], None]


class EventStoreError(RuntimeError):
    """The event store could not complete an operation.

    Raised rather than swallowed. If events cannot be durably recorded the pipeline must
    halt, because continuing would produce confident output that cannot afterwards be
    reconstructed or defended. See docs/failure_modes.md FM-032.
    """


class EventStore(ABC):
    """The append-only log interface.

    Note what is absent: no update, no delete, no truncate. The interface itself refuses to
    express the operations that would break the log's guarantees.
    """

    @abstractmethod
    def append(
        self,
        event_type: EventType,
        source: str,
        payload: dict[str, Any] | None = None,
        track_id: str | None = None,
    ) -> Event:
        """Durably append one event and return it with its assigned `seq`.

        Returns only after the event is committed. Subscribers are notified after the
        commit, never before.
        """

    @abstractmethod
    def read_from(self, seq: int, limit: int | None = None) -> Iterator[Event]:
        """Events with sequence number greater than or equal to `seq`, in order."""

    @abstractmethod
    def read_range(self, from_seq: int, to_seq: int) -> Iterator[Event]:
        """Events in the inclusive sequence range, in order."""

    @abstractmethod
    def latest_seq(self) -> int:
        """Highest assigned sequence number, or 0 if the log is empty."""

    @abstractmethod
    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        """Register a callback invoked after each commit. Returns an unsubscribe callable."""

    @abstractmethod
    def close(self) -> None:
        """Release resources. The log itself persists."""


class SqliteEventStore(EventStore):
    """SQLite-backed event log.

    WAL mode gives a single writer with concurrent readers and durable commits without a
    server process — the exact shape required, at the scale required.

    Immutability is enforced by database triggers rather than only by this class's interface.
    A future module that reaches past this API and issues an UPDATE or DELETE against the
    events table gets an error from SQLite itself. The guarantee should not depend on every
    caller being well behaved.

    Not thread-safe, by design. Single-writer is the architecture (ADR 0006), so a lock here
    would advertise a concurrency model Aetos deliberately does not have.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS events (
        seq        INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id   TEXT    NOT NULL UNIQUE,
        timestamp  TEXT    NOT NULL,
        event_type TEXT    NOT NULL,
        source     TEXT    NOT NULL,
        track_id   TEXT,
        payload    TEXT    NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_events_track ON events (track_id, seq);
    CREATE INDEX IF NOT EXISTS idx_events_type  ON events (event_type, seq);

    -- The append-only guarantee, enforced by the storage engine rather than by convention.
    CREATE TRIGGER IF NOT EXISTS events_are_immutable
    BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'event log is append-only: events cannot be updated');
    END;

    CREATE TRIGGER IF NOT EXISTS events_cannot_be_deleted
    BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'event log is append-only: events cannot be deleted');
    END;
    """

    def __init__(self, clock: Clock, path: Path | str | None = None) -> None:
        """Open or create a log.

        `path` of None gives an in-memory log, used by tests. In-memory has no durability
        and cannot use WAL, so it is never appropriate for anything but tests — the
        distinction is recorded on the instance rather than left implicit.
        """
        self._clock = clock
        self._ids = event_id_generator()
        self._subscribers: list[Subscriber] = []
        self._path = Path(path) if path is not None else None
        self.is_durable = self._path is not None

        try:
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)

            self._conn = sqlite3.connect(
                str(self._path) if self._path else ":memory:",
                isolation_level=None,  # explicit transaction control
                # Every reader and the writer run on the event-loop thread, but that is not
                # always the thread that opened the store — a test client runs the application
                # on a thread of its own, and Python's same-thread check would reject it.
                #
                # This relaxes the ownership check only. The single-writer guarantee (ADR 0006)
                # is an architectural discipline about who appends, not a property of Python's
                # thread assertion, and nothing here licenses a second writer. SQLite
                # serialises access internally, so concurrent readers alongside the writer are
                # exactly what WAL mode is for.
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            if self.is_durable:
                # WAL: concurrent readers alongside the single writer.
                self._conn.execute("PRAGMA journal_mode=WAL")
                # FULL rather than NORMAL: an event must survive process loss, since the log
                # is the only authority on what happened. This is the durability the
                # write-before-read guarantee actually rests on.
                self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(self._SCHEMA)
        except (sqlite3.Error, OSError) as exc:
            # Includes filesystem failures creating the containing directory. Every way the
            # store can fail to open must surface as EventStoreError, because the caller's
            # correct response is the same in all of them: halt (FM-032).
            raise EventStoreError(f"could not open event store: {exc}") from exc

        # Resume identifier numbering past whatever is already on disk, so reopening an
        # existing log does not generate event IDs that collide with recorded ones.
        self._restore_id_sequence()

    def append(
        self,
        event_type: EventType,
        source: str,
        payload: dict[str, Any] | None = None,
        track_id: str | None = None,
    ) -> Event:
        payload = payload or {}

        # Serialise before opening the transaction. A payload that cannot be represented is
        # a programming error, and it should surface as one rather than as a half-written
        # transaction.
        try:
            encoded = json.dumps(payload, separators=(",", ":"), default=_json_default)
        except (TypeError, ValueError) as exc:
            raise EventStoreError(
                f"payload for {event_type} from {source} is not JSON-serialisable: {exc}"
            ) from exc

        timestamp = self._clock.now()
        event_id = self._ids.next()

        try:
            cursor = self._conn.execute(
                "INSERT INTO events (event_id, timestamp, event_type, source, track_id, payload)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, timestamp.isoformat(), str(event_type), source, track_id, encoded),
            )
        except sqlite3.Error as exc:
            raise EventStoreError(f"could not append {event_type}: {exc}") from exc

        event = Event(
            seq=cursor.lastrowid,
            event_id=event_id,
            timestamp=timestamp,
            event_type=event_type,
            source=source,
            track_id=track_id,
            payload=payload,
        )

        self._notify(event)
        return event

    def read_from(self, seq: int, limit: int | None = None) -> Iterator[Event]:
        sql = "SELECT * FROM events WHERE seq >= ? ORDER BY seq"
        params: tuple[Any, ...] = (seq,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        yield from self._query(sql, params)

    def read_range(self, from_seq: int, to_seq: int) -> Iterator[Event]:
        yield from self._query(
            "SELECT * FROM events WHERE seq >= ? AND seq <= ? ORDER BY seq", (from_seq, to_seq)
        )

    def read_for_track(self, track_id: str) -> Iterator[Event]:
        """Every event concerning one track, in order. The track's full history."""
        yield from self._query("SELECT * FROM events WHERE track_id = ? ORDER BY seq", (track_id,))

    def latest_seq(self) -> int:
        row = self._conn.execute("SELECT MAX(seq) AS max_seq FROM events").fetchone()
        return row["max_seq"] or 0

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

        return unsubscribe

    def close(self) -> None:
        self._conn.close()

    def _notify(self, event: Event) -> None:
        """Notify subscribers after the commit.

        A failing subscriber must not roll back or mask a committed event — the event
        happened, and the log's account of it is already authoritative. The failure is
        surfaced as its own event so it is visible rather than swallowed, and the remaining
        subscribers still run.
        """
        for subscriber in list(self._subscribers):
            try:
                subscriber(event)
            except Exception as exc:  # noqa: BLE001 - deliberately broad; see docstring
                self._record_subscriber_failure(subscriber, event, exc)

    def _record_subscriber_failure(
        self, subscriber: Subscriber, event: Event, exc: Exception
    ) -> None:
        """Record a subscriber failure without recursing into the same failure path."""
        encoded = json.dumps(
            {
                "failed_subscriber": getattr(subscriber, "__qualname__", repr(subscriber)),
                "during_event_seq": event.seq,
                "during_event_type": str(event.event_type),
                "error": f"{type(exc).__name__}: {exc}",
            },
            separators=(",", ":"),
        )
        try:
            self._conn.execute(
                "INSERT INTO events (event_id, timestamp, event_type, source, track_id, payload)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self._ids.next(),
                    self._clock.now().isoformat(),
                    str(EventType.SUBSCRIBER_FAILED),
                    "event_store",
                    None,
                    encoded,
                ),
            )
        except sqlite3.Error:
            # If the log itself cannot be written, the pipeline is already in the FM-032
            # state and the original append will have raised. Nothing further to do here.
            pass

    def _query(self, sql: str, params: tuple[Any, ...]) -> Iterator[Event]:
        try:
            for row in self._conn.execute(sql, params):
                yield _row_to_event(row)
        except sqlite3.Error as exc:
            raise EventStoreError(f"could not read events: {exc}") from exc

    def _restore_id_sequence(self) -> None:
        count = self.count()
        if count:
            self._ids = event_id_generator()
            for _ in range(count):
                self._ids.next()


def _row_to_event(row: sqlite3.Row) -> Event:
    from datetime import datetime

    return Event(
        seq=row["seq"],
        event_id=row["event_id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        event_type=EventType(row["event_type"]),
        source=row["source"],
        track_id=row["track_id"],
        payload=json.loads(row["payload"]),
    )


def _json_default(value: Any) -> Any:
    """Serialise the types payloads legitimately carry.

    Deliberately narrow. Falling back to `str()` for anything unrecognised would let objects
    into the log as unparseable text, and replay would then read back something that is not
    what was written.
    """
    if hasattr(value, "model_dump"):  # pydantic models
        return value.model_dump(mode="json")
    if hasattr(value, "isoformat"):  # datetime, date
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return sorted(value)
    raise TypeError(f"object of type {type(value).__name__} is not serialisable into an event")
