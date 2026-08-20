"""SQLite connection handling for the persistence layer.

Deliberately the same posture as `backend/events/store.py`: WAL mode, `synchronous=FULL`,
one connection. WAL gives a single writer with concurrent readers and durable commits without
a server process (ADR 0006).

One lock, and only one: `transaction()` serialises the BEGIN/COMMIT pair across threads. This
file used to say it held no lock at all, on the grounds that a lock would advertise a
concurrency model Aetos does not have. The connection is nevertheless shared by an API
threadpool, so two threads really did open transactions at once — and the resulting failure
lost writes. Enforcing the single-writer discipline is not the same as advertising a
concurrent one.

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
import threading
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

    Reads are safe from any thread — SQLite serialises statements internally, and the
    same-thread check is relaxed below. Writes are serialised by `transaction()`, which holds a
    lock for the whole unit of work so the single-writer discipline holds in fact and not only
    in intent.
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
        # Guards `_depth` and the BEGIN/COMMIT pair it controls. Reentrant, so a caller
        # composing two repository operations on one thread still nests by depth rather than
        # deadlocking. See `transaction`.
        self._lock = threading.RLock()
        self.is_durable = self._path is not None

        try:
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)

            self._conn = sqlite3.connect(
                str(self._path) if self._path else ":memory:",
                isolation_level=None,  # explicit transaction control
                # The API serves synchronous endpoints from a threadpool, so the thread that
                # reads a repository is not the thread that opened it. Python's default
                # same-thread check would reject those reads — in production as well as in
                # tests, since that is simply how the server runs.
                #
                # This relaxes the *ownership* check. The concurrency discipline is
                # enforced by the lock in `transaction()` rather than by thread affinity —
                # relying on affinity alone was the bug, because affinity was already
                # relaxed here and nothing took its place.
                check_same_thread=False,
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

        **Held under a lock, and it has to be.** The connection is opened with
        `check_same_thread=False` because the API serves requests from a threadpool, so two
        threads genuinely do reach this method at once. Depth-counting alone is a check-then-act
        race: both threads read `_depth == 0`, both issue `BEGIN IMMEDIATE`, and the second gets
        "cannot start a transaction within a transaction".

        That is not theoretical. `sessions.touch()` runs on *every authenticated request* to
        slide the idle expiry, so any two concurrent requests could collide — observed as a 500
        from `GET /system/health` during an ordinary sign-in. Reproduced directly: eight threads
        committing sixty rows each raised six times and **lost six writes**, because a
        transaction that fails to begin takes its statements with it.

        SQLite serialises individual statements; it does not serialise a read-modify-write of a
        Python attribute, nor the pairing of a BEGIN with its COMMIT. The lock is what makes the
        single-writer discipline this file claims (ADR 0006) actually true rather than merely
        intended.
        """
        with self._lock:
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
