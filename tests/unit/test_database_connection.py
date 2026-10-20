"""The persistence connection's one-thread-at-a-time rule, enforced rather than assumed.

The connection has no lock by design (ADR 0006), so it is sound only while no two threads use
it at once. When they did — a session dependency running in FastAPI's thread pool — a second
thread's statements silently joined the first thread's open transaction. These tests pin the
guard that turns that into a loud, named failure, and check it does not refuse the sequential
cross-thread use a test client legitimately makes.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from backend.database.connection import Database, DatabaseError


@pytest.fixture
def db() -> Database:
    database = Database(SimClock(datetime(2026, 1, 1, tzinfo=UTC)))
    database.apply_schema("CREATE TABLE t (v INTEGER)")
    yield database
    database.close()


def run_in_other_thread(work) -> BaseException | None:
    caught: list[BaseException] = []

    def target() -> None:
        try:
            work()
        except BaseException as exc:  # noqa: BLE001 - reported back to the test thread
            caught.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    return caught[0] if caught else None


def test_a_second_thread_cannot_write_into_an_open_transaction(db: Database) -> None:
    """The failure that used to be silent: the other thread's write would have joined this
    transaction and been rolled back with it, by code that never knew it existed."""
    with pytest.raises(RuntimeError, match="rolled back"):
        with db.transaction():
            db.execute("INSERT INTO t VALUES (1)")
            error = run_in_other_thread(lambda: db.execute("INSERT INTO t VALUES (2)"))
            assert isinstance(error, DatabaseError)
            assert "second thread" in str(error)
            raise RuntimeError("rolled back")

    assert db.query("SELECT v FROM t") == [], "nothing from either thread may survive the rollback"


def test_a_second_thread_cannot_open_its_own_transaction_meanwhile(db: Database) -> None:
    """The exact production symptom — `BEGIN IMMEDIATE` inside a transaction — now named."""
    with db.transaction():
        error = run_in_other_thread(lambda: db.transaction().__enter__())
    assert isinstance(error, DatabaseError)
    assert "event loop" in str(error)


def test_sequential_use_from_another_thread_is_allowed(db: Database) -> None:
    """What a test client does: the app runs on its own thread, one request at a time. No
    transaction is open when it arrives, so there is nothing to collide with."""
    error = run_in_other_thread(lambda: db.execute("INSERT INTO t VALUES (3)"))
    assert error is None
    assert [row["v"] for row in db.query("SELECT v FROM t")] == [3]


def test_the_owning_thread_may_nest_transactions_as_before(db: Database) -> None:
    with db.transaction():
        with db.transaction():
            db.execute("INSERT INTO t VALUES (4)")
    assert [row["v"] for row in db.query("SELECT v FROM t")] == [4]
