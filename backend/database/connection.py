"""SQLite connection handling for the persistence layer.

Deliberately the same posture as `backend/events/store.py`: WAL mode, `synchronous=FULL`,
one connection, no lock. The reasoning transfers directly — WAL gives a single writer with
concurrent readers and durable commits without a server process, and a lock here would
advertise a concurrency model Aetos does not have (ADR 0006).

`synchronous=FULL` is arguably generous for the track tables, which are a projection and can
always be rebuilt from the log. It is kept because the same file also holds sites, sensors
and scenario definitions, which are *not* derivable from the log and would be genuinely lost.
Two durability postures in one file would be a footgun for whoever adds the next table.

The persistence database is a separate file from the event log, always. Rebuilding or
discarding the projection is a routine operation; it must be structurally incapable of
touching the log it is derived from.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.common.clock import Clock


class DatabaseError(RuntimeError):
    """The persistence layer could not complete an operation.

    Raised rather than swallowed. Unlike the event log (FM-032), losing the projection is not
    fatal — it can be rebuilt — but a *silent* failure would leave the API serving a
    projection that has quietly stopped tracking the log, which is worse than an error.
    """


class Database:
    """One SQLite connection, shared by every repository built over it.

    Not thread-safe, and opened with SQLite's default thread affinity. That is a real
    constraint on callers rather than an oversight: repository reads must happen on the
    thread that opened the connection, which is why the API's route handlers are `async def`
    and read inline instead of being dispatched to a worker thread.
    """

    def __init__(self, clock: Clock, path: Path | str | None = None) -> None:
        """Open or create the database.

        `path` of None gives an in-memory database with no durability and no WAL. That is for
        tests only, and the instance reports `is_durable == False` rather than leaving the
        distinction implicit.
        """
        self._clock = clock
        self._path = Path(path) if path is not None else None
        self._depth = 0
        self.is_durable = self._path is not None

        try:
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)

            self._conn = sqlite3.connect(
                str(self._path) if self._path else ":memory:",
                isolation_level=None,  # explicit transaction control
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            if self.is_durable:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=FULL")
        except (sqlite3.Error, OSError) as exc:
            raise DatabaseError(f"could not open database: {exc}") from exc

    @property
    def clock(self) -> Clock:
        return self._clock

    def now(self) -> datetime:
        """Current time from the injected clock. Nothing here reads wall time (ADR 0003)."""
        return self._clock.now()

    def apply_schema(self, schema: str) -> None:
        try:
            self._conn.executescript(schema)
        except sqlite3.Error as exc:
            raise DatabaseError(f"could not apply schema: {exc}") from exc

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        try:
            return self._conn.execute(sql, params)
        except sqlite3.Error as exc:
            raise DatabaseError(f"statement failed: {exc}") from exc

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group statements into one unit of work.

        Reentrant, counting depth rather than nesting savepoints: a caller composing two
        repository operations should not have to know whether either of them already opened a
        transaction. Only the outermost block commits.
        """
        if self._depth == 0:
            self.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()
