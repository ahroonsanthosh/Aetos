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

    **Used from one thread at a time, and in practice from the event-loop thread only.** The
    pipeline steps on the loop, and every API handler and dependency is `async def`, so they run
    on it too. That is the whole concurrency model, and it is why there is no lock here: a lock
    would advertise concurrent use the architecture does not have (ADR 0006).

    Break it and the failure is worse than an error. A second thread issuing statements while
    another holds a transaction does not wait — it joins that transaction, and its writes commit
    or roll back with someone else's work. This happened: the session dependency was a plain
    `def`, FastAPI ran it in a worker thread, and the console's opening burst of requests
    collided here. The guard in `execute` now refuses that loudly instead, and
    `tests/integration/test_concurrent_requests.py` keeps every dependency off the thread pool.
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
        self._transaction_thread: int | None = None
        """The thread holding the open transaction, if any. See `_refuse_other_threads`."""
        self.is_durable = self._path is not None

        try:
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)

            self._conn = sqlite3.connect(
                str(self._path) if self._path else ":memory:",
                isolation_level=None,  # explicit transaction control
                # The thread that uses the connection is not always the thread that opened it:
                # a test client runs the application on a thread of its own. This relaxes
                # Python's *ownership* check and nothing else — it is not a licence for two
                # threads at once, which is what the guard in `execute` enforces.
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

    def _refuse_other_threads(self) -> None:
        """Fail loudly if another thread's transaction is open on this connection.

        Without this, a statement from a second thread does not wait or fail — it silently
        joins the first thread's transaction, committing or rolling back with work it knows
        nothing about. SQLite's own complaint, when it complains at all, is "cannot start a
        transaction within a transaction", which names the symptom and hides the cause.
        """
        if self._depth and self._transaction_thread != threading.get_ident():
            raise DatabaseError(
                "the database was used from a second thread while another thread's transaction "
                "was open. This connection is single-threaded by design: run the caller on the "
                "event loop (an `async def` handler or dependency), not in a worker thread."
            )

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        self._refuse_other_threads()
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
        self._refuse_other_threads()
        if self._depth == 0:
            self.execute("BEGIN IMMEDIATE")
            self._transaction_thread = threading.get_ident()
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.execute("ROLLBACK")
                self._transaction_thread = None
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.execute("COMMIT")
                self._transaction_thread = None

    def close(self) -> None:
        self._conn.close()
