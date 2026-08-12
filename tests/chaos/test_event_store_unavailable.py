"""FM-032 — the event store cannot be written.

Nothing downstream may act on an event before it is durably appended (ADR 0006), so losing the
ability to append halts the pipeline by design. That part is not in question. What these tests
are about is the *second* failure that follows it, which is far more dangerous than the first.

When the pipeline stops, everything else keeps working. The HTTP server answers. The WebSocket
stays open. The projection serves the tracks it already has. None of those depend on a cycle
having run, so all of them go on presenting the last good picture — indefinitely, and with no
indication anywhere that it is frozen. A console that has stopped updating while continuing to
look authoritative is worse than one that has visibly crashed, because only one of the two gets
investigated.

The failure is injected with `PRAGMA query_only`, which is the honest shape of this outage:
writes fail, reads keep succeeding. A test that closed the connection instead would break reads
too, and would therefore never catch a health check that only reads — which is exactly the bug
that was here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from backend.events.store import EventStoreError, SqliteEventStore
from backend.events.types import EventType
from backend.system_health.cycles import (
    UNAVAILABLE_AFTER_FAILURES,
    CycleHealth,
)

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> SimClock:
    return SimClock(START)


@pytest.fixture
def store(clock: SimClock):
    store = SqliteEventStore(clock)
    yield store
    store.close()


def make_writes_fail(store: SqliteEventStore) -> None:
    """Take away the ability to append, leaving the ability to read.

    A read-only filesystem, a full disk and a revoked grant all present this way, and it is the
    combination that makes FM-032 hard to notice: every diagnostic that only reads still says
    the store is fine.
    """
    store._conn.execute("PRAGMA query_only = ON")


def allow_writes_again(store: SqliteEventStore) -> None:
    store._conn.execute("PRAGMA query_only = OFF")


def test_a_failed_append_raises_rather_than_returning_quietly(store: SqliteEventStore) -> None:
    """The whole architecture rests on an append having happened.

    An `append` that failed silently would let every module downstream act on an observation
    that is not in the log — producing tracks, classifications and alerts that cannot be
    reconstructed or defended afterwards, which is the one outcome ADR 0006 exists to prevent.
    """
    make_writes_fail(store)

    with pytest.raises(EventStoreError):
        store.append(EventType.DETECTION_RECEIVED, "chaos", payload={"detection_id": "det-1"})


def test_the_log_keeps_reading_while_it_cannot_be_written(store: SqliteEventStore) -> None:
    """The property that makes this outage deceptive, pinned down so the tests below mean
    something. If reads failed too, every health check would already have caught it."""
    store.append(EventType.DETECTION_RECEIVED, "chaos", payload={"detection_id": "det-before"})
    make_writes_fail(store)

    assert store.latest_seq() >= 1
    assert [event.event_type for event in store.read_from(1)] == [EventType.DETECTION_RECEIVED]


def test_a_write_outage_does_not_corrupt_the_identifier_sequence(clock: SimClock, tmp_path) -> None:
    """The bug this test exists for, which only appears one restart later.

    An append draws its identifier *before* the row is written, so a failure burns that
    identifier permanently. If the sequence were then restored by counting rows — as it was —
    the count would be one short of the high-water mark from that moment on, and the next
    append after a restart would re-issue an identifier the log already holds. `event_id` is
    UNIQUE, so the store would refuse it, and one transient write failure would become a
    permanent inability to append that *survives the restart meant to clear it*.

    The gap must therefore be preserved rather than closed up.
    """
    path = tmp_path / "events.db"

    store = SqliteEventStore(clock, path)
    store.append(EventType.DETECTION_RECEIVED, "chaos", payload={"n": 1})
    store.append(EventType.DETECTION_RECEIVED, "chaos", payload={"n": 2})

    make_writes_fail(store)
    with pytest.raises(EventStoreError):
        store.append(EventType.DETECTION_RECEIVED, "chaos", payload={"n": "burned"})
    allow_writes_again(store)

    store.append(EventType.DETECTION_RECEIVED, "chaos", payload={"n": 3})
    before_restart = [event.event_id for event in store.read_from(1)]
    store.close()

    # The restart.
    reopened = SqliteEventStore(clock, path)
    try:
        reopened.append(EventType.DETECTION_RECEIVED, "chaos", payload={"n": 4})
        after_restart = [event.event_id for event in reopened.read_from(1)]
    finally:
        reopened.close()

    assert len(set(after_restart)) == len(after_restart), (
        f"an identifier was reissued after the restart: {after_restart}"
    )
    assert after_restart[: len(before_restart)] == before_restart


def test_health_reports_a_frozen_pipeline_instead_of_reporting_nothing() -> None:
    """The visible half of "fail loudly and visibly".

    `CycleHealth` is what the health endpoint reads. Before it existed the consecutive-failure
    count lived only in a log line, so a pipeline that had stopped entirely was reported as
    healthy by every surface a monitoring probe or an operator can actually reach.
    """
    cycles = CycleHealth()
    cycles.record_success(START)
    assert cycles.is_healthy

    cycles.record_failure(START, EventStoreError("attempt to write a readonly database"))

    assert not cycles.is_healthy, "one failed cycle already means the picture is not advancing"
    assert not cycles.is_outage, "a single failure is not yet an outage"
    assert "not advancing" in cycles.describe().lower()
    assert "readonly" in cycles.describe()


def test_a_sustained_outage_is_distinguishable_from_one_bad_cycle() -> None:
    """Both are worth reporting; only one is worth waking someone up for.

    A single failure can be a momentary lock the next tick clears. Sustained failure means
    nothing is clearing it, and collapsing the two into one state would either page on every
    transient blip or stay quiet through a real outage.
    """
    cycles = CycleHealth()
    for _ in range(UNAVAILABLE_AFTER_FAILURES - 1):
        cycles.record_failure(START, EventStoreError("disk I/O error"))
    assert not cycles.is_healthy
    assert not cycles.is_outage

    cycles.record_failure(START, EventStoreError("disk I/O error"))
    assert cycles.is_outage


def test_recovery_clears_the_alarm_rather_than_latching_it() -> None:
    """A store that comes back must stop being reported as broken.

    Latching would be its own hazard: an operator who has learned the banner stays up after the
    fault clears is an operator who stops reading the banner.
    """
    cycles = CycleHealth()
    for _ in range(UNAVAILABLE_AFTER_FAILURES):
        cycles.record_failure(START, EventStoreError("disk I/O error"))
    assert cycles.is_outage

    cycles.record_success(START)

    assert cycles.is_healthy
    assert not cycles.is_outage
