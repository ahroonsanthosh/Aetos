"""The persistence connection, and the concurrency discipline it is responsible for.

The connection is opened with `check_same_thread=False` because the API serves requests from a
threadpool. That is a deliberate choice, but it means thread affinity is *not* what keeps writes
orderly — something else has to, and for a while nothing did.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from backend.database.connection import Database, DatabaseError

EPOCH = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def db() -> Database:
    database = Database(SimClock(EPOCH))
    database.apply_schema("CREATE TABLE widget (k INTEGER PRIMARY KEY, v TEXT NOT NULL);")
    yield database
    database.close()


def test_concurrent_transactions_neither_fail_nor_lose_writes(db: Database) -> None:
    """Eight threads, sixty writes each, and all 480 must survive.

    This is a regression test for a live defect, not a hypothetical. `transaction()` counted
    depth on a plain attribute, so two threads both read `_depth == 0`, both issued
    `BEGIN IMMEDIATE`, and the second raised "cannot start a transaction within a transaction".

    The failure was found during an ordinary sign-in — `sessions.touch()` slides the idle expiry
    on *every authenticated request*, so two concurrent requests were enough, and it surfaced as
    a 500 from `GET /system/health`. The lost writes are the worse half: a transaction that fails
    to begin takes its statements with it, and this loop lost six rows before the fix.
    """
    threads_count, per_thread = 8, 60
    barrier = threading.Barrier(threads_count)
    failures: list[str] = []

    def worker(worker_id: int) -> None:
        barrier.wait()  # maximise the overlap rather than hoping for it
        for j in range(per_thread):
            try:
                with db.transaction():
                    db.execute(
                        "INSERT INTO widget (k, v) VALUES (?, ?)",
                        (worker_id * 1000 + j, f"w{worker_id}"),
                    )
            except DatabaseError as exc:  # noqa: PERF203 - the failure is the subject
                failures.append(str(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert failures == []
    row = db.query_one("SELECT COUNT(*) AS c FROM widget")
    assert row is not None
    assert row["c"] == threads_count * per_thread


def test_a_nested_transaction_on_one_thread_still_commits_once(db: Database) -> None:
    """The lock is reentrant, so composing repository calls does not deadlock.

    This is the property depth-counting existed for in the first place, and adding a lock is
    exactly the change that could have broken it.
    """
    with db.transaction():
        db.execute("INSERT INTO widget (k, v) VALUES (1, 'outer')")
        with db.transaction():
            db.execute("INSERT INTO widget (k, v) VALUES (2, 'inner')")

    rows = db.query("SELECT k FROM widget ORDER BY k")
    assert [r["k"] for r in rows] == [1, 2]


def test_a_failure_inside_a_nested_block_rolls_the_whole_unit_back(db: Database) -> None:
    """Only the outermost block commits, so an inner failure discards the outer work too.

    Half-applied units of work are the thing transactions exist to prevent, and a lock that
    released early would leave the rollback racing another thread's BEGIN.
    """
    with pytest.raises(DatabaseError):
        with db.transaction():
            db.execute("INSERT INTO widget (k, v) VALUES (1, 'outer')")
            with db.transaction():
                # NOT NULL violation.
                db.execute("INSERT INTO widget (k, v) VALUES (2, NULL)")

    row = db.query_one("SELECT COUNT(*) AS c FROM widget")
    assert row is not None
    assert row["c"] == 0


def test_the_connection_is_usable_again_after_a_rolled_back_transaction(db: Database) -> None:
    """A failed unit must not leave the depth counter or the lock wedged.

    The event store had exactly this class of defect — a transient failure that poisoned every
    later write — so it is worth pinning here too.
    """
    with pytest.raises(DatabaseError):
        with db.transaction():
            db.execute("INSERT INTO widget (k, v) VALUES (1, NULL)")

    with db.transaction():
        db.execute("INSERT INTO widget (k, v) VALUES (2, 'after')")

    rows = db.query("SELECT k FROM widget")
    assert [r["k"] for r in rows] == [2]
