"""Event store: the four guarantees of ADR 0006, and what happens when they are stressed.

1. Single writer  2. Monotonic seq  3. Write-before-read  4. Immutable
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.common.clock import SimClock
from backend.events.store import EventStoreError, SqliteEventStore
from backend.events.types import Event, EventType

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> SimClock:
    return SimClock(START)


@pytest.fixture
def store(clock: SimClock) -> SqliteEventStore:
    s = SqliteEventStore(clock)
    yield s
    s.close()


# --------------------------------------------------------------------------------------
# Guarantee 2: monotonic sequence — the total order of everything that happened
# --------------------------------------------------------------------------------------


def test_sequence_starts_at_one_and_increments(store: SqliteEventStore) -> None:
    events = [
        store.append(EventType.TRACK_CREATED, "tracking", track_id=f"TRK-0000{i}")
        for i in range(1, 4)
    ]

    assert [e.seq for e in events] == [1, 2, 3]


def test_latest_seq_of_empty_log_is_zero(store: SqliteEventStore) -> None:
    assert store.latest_seq() == 0


def test_read_order_follows_sequence_even_when_timestamps_are_identical(
    store: SqliteEventStore,
) -> None:
    """Sensors' clocks may disagree (FM-012), and events from different sources can share a
    timestamp exactly. `seq` is assigned in one place by one writer, so it is the only
    ordering that cannot be argued with — read order follows it, never the timestamp."""
    first = store.append(EventType.DETECTION_RECEIVED, "sensor-a")
    second = store.append(EventType.DETECTION_RECEIVED, "sensor-b")

    assert first.timestamp == second.timestamp
    assert second.seq > first.seq
    assert [e.source for e in store.read_from(1)] == ["sensor-a", "sensor-b"]


def test_sequence_never_reused_after_reopen(tmp_path: Path, clock: SimClock) -> None:
    """AUTOINCREMENT rather than max(seq)+1: a reused sequence number would make two
    different events indistinguishable in the total order."""
    path = tmp_path / "events.db"

    store = SqliteEventStore(clock, path)
    store.append(EventType.SYSTEM_STARTED, "system")
    store.append(EventType.SYSTEM_STARTED, "system")
    store.close()

    reopened = SqliteEventStore(clock, path)
    third = reopened.append(EventType.SYSTEM_STARTED, "system")

    assert third.seq == 3
    assert reopened.latest_seq() == 3
    reopened.close()


def test_event_ids_do_not_collide_after_reopen(tmp_path: Path, clock: SimClock) -> None:
    path = tmp_path / "events.db"

    store = SqliteEventStore(clock, path)
    first_ids = {store.append(EventType.SYSTEM_STARTED, "system").event_id for _ in range(3)}
    store.close()

    reopened = SqliteEventStore(clock, path)
    new_id = reopened.append(EventType.SYSTEM_STARTED, "system").event_id

    assert new_id not in first_ids
    reopened.close()


# --------------------------------------------------------------------------------------
# Guarantee 4: immutability — enforced by the storage engine, not by convention
# --------------------------------------------------------------------------------------


def test_interface_offers_no_way_to_mutate_the_log() -> None:
    """The interface refuses to express the operations that would break the guarantees."""
    forbidden = {"update", "delete", "truncate", "remove", "clear", "purge"}

    assert not forbidden & {name for name in dir(SqliteEventStore) if not name.startswith("_")}


def test_direct_update_is_rejected_by_the_database(store: SqliteEventStore) -> None:
    """Bypassing the API must not bypass the guarantee. A future module that reaches past
    this class gets an error from SQLite itself."""
    store.append(EventType.TRACK_CREATED, "tracking", track_id="TRK-00001")

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE events SET source = 'tampered' WHERE seq = 1")


def test_direct_delete_is_rejected_by_the_database(store: SqliteEventStore) -> None:
    store.append(EventType.TRACK_CREATED, "tracking", track_id="TRK-00001")

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM events WHERE seq = 1")

    assert store.count() == 1


def test_returned_events_are_immutable(store: SqliteEventStore) -> None:
    event = store.append(EventType.TRACK_CREATED, "tracking")

    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError on frozen
        event.seq = 99


# --------------------------------------------------------------------------------------
# Guarantee 3: write-before-read
# --------------------------------------------------------------------------------------


def test_subscribers_are_notified_only_after_commit(store: SqliteEventStore) -> None:
    """The event must be durably readable at the moment a subscriber sees it — otherwise a
    consumer could act on something not yet recorded, and replay would not match live."""
    seen_and_readable: list[bool] = []

    def subscriber(event: Event) -> None:
        readable = [e.seq for e in store.read_from(event.seq)]
        seen_and_readable.append(event.seq in readable)

    store.subscribe(subscriber)
    store.append(EventType.TRACK_CREATED, "tracking")

    assert seen_and_readable == [True]


def test_append_returns_only_after_the_event_is_readable(store: SqliteEventStore) -> None:
    event = store.append(EventType.TRACK_CREATED, "tracking")

    assert [e.seq for e in store.read_from(event.seq)] == [event.seq]


def test_unsubscribe_stops_delivery(store: SqliteEventStore) -> None:
    received: list[Event] = []
    unsubscribe = store.subscribe(received.append)

    store.append(EventType.TRACK_CREATED, "tracking")
    unsubscribe()
    store.append(EventType.TRACK_UPDATED, "tracking")

    assert [e.event_type for e in received] == [EventType.TRACK_CREATED]


def test_failing_subscriber_does_not_lose_the_event(store: SqliteEventStore) -> None:
    """The event happened. A consumer failing to process it does not change that, and must
    not roll back a committed record."""

    def broken(event: Event) -> None:
        raise RuntimeError("consumer exploded")

    store.subscribe(broken)
    event = store.append(EventType.TRACK_CREATED, "tracking")

    assert [e.seq for e in store.read_from(event.seq)][0] == event.seq


def test_failing_subscriber_does_not_block_the_others(store: SqliteEventStore) -> None:
    received: list[Event] = []

    def broken(event: Event) -> None:
        raise RuntimeError("consumer exploded")

    store.subscribe(broken)
    store.subscribe(received.append)
    store.append(EventType.TRACK_CREATED, "tracking")

    assert len(received) == 1


def test_subscriber_failure_is_recorded_rather_than_swallowed(store: SqliteEventStore) -> None:
    """A silent consumer failure is the worst kind: the system looks healthy while a
    downstream stage is receiving nothing."""

    def broken(event: Event) -> None:
        raise RuntimeError("consumer exploded")

    store.subscribe(broken)
    store.append(EventType.TRACK_CREATED, "tracking")

    failures = [e for e in store.read_from(1) if e.event_type is EventType.SUBSCRIBER_FAILED]
    assert len(failures) == 1
    assert "consumer exploded" in failures[0].payload["error"]
    assert failures[0].payload["during_event_type"] == "TRACK_CREATED"


# --------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------


def test_read_from_returns_events_in_sequence_order(store: SqliteEventStore) -> None:
    for i in range(5):
        store.append(EventType.TRACK_UPDATED, "tracking", payload={"i": i})

    assert [e.payload["i"] for e in store.read_from(3)] == [2, 3, 4]


def test_read_from_respects_limit(store: SqliteEventStore) -> None:
    for _ in range(10):
        store.append(EventType.TRACK_UPDATED, "tracking")

    assert len(list(store.read_from(1, limit=4))) == 4


def test_read_range_is_inclusive_at_both_ends(store: SqliteEventStore) -> None:
    for i in range(5):
        store.append(EventType.TRACK_UPDATED, "tracking", payload={"i": i})

    assert [e.seq for e in store.read_range(2, 4)] == [2, 3, 4]


def test_read_for_track_returns_that_track_only(store: SqliteEventStore) -> None:
    store.append(EventType.TRACK_CREATED, "tracking", track_id="TRK-00001")
    store.append(EventType.TRACK_CREATED, "tracking", track_id="TRK-00002")
    store.append(EventType.TRACK_UPDATED, "tracking", track_id="TRK-00001")
    store.append(EventType.DETECTION_RECEIVED, "sensor-a")

    history = list(store.read_for_track("TRK-00001"))

    assert [e.event_type for e in history] == [EventType.TRACK_CREATED, EventType.TRACK_UPDATED]


def test_reading_past_the_end_yields_nothing(store: SqliteEventStore) -> None:
    store.append(EventType.SYSTEM_STARTED, "system")

    assert list(store.read_from(99)) == []


# --------------------------------------------------------------------------------------
# Payloads and timestamps
# --------------------------------------------------------------------------------------


def test_payload_round_trips_through_storage(store: SqliteEventStore) -> None:
    payload = {
        "risk_score": 81,
        "risk_level": "HIGH",
        "reasons": ["approaching_protected_zone", "persistent_track"],
        "position": {"latitude": 53.35, "longitude": -6.26, "altitude_m": 120.0},
        "confirmed": True,
        "previous_level": None,
    }

    store.append(EventType.RISK_CHANGED, "risk", payload=payload, track_id="TRK-00001")

    assert next(store.read_from(1)).payload == payload


def test_datetimes_in_payloads_are_serialised(store: SqliteEventStore) -> None:
    store.append(EventType.TRACK_LOST, "tracking", payload={"last_seen": START})

    assert next(store.read_from(1)).payload["last_seen"] == START.isoformat()


def test_unserialisable_payload_fails_loudly(store: SqliteEventStore) -> None:
    """Falling back to str() would let objects into the log as unparseable text, and replay
    would then read back something that is not what was written."""

    class Opaque:
        pass

    with pytest.raises(EventStoreError, match="not JSON-serialisable"):
        store.append(EventType.TRACK_CREATED, "tracking", payload={"thing": Opaque()})


def test_unserialisable_payload_does_not_advance_the_sequence(store: SqliteEventStore) -> None:
    """A rejected append must leave no gap in the total order."""

    class Opaque:
        pass

    store.append(EventType.SYSTEM_STARTED, "system")
    with pytest.raises(EventStoreError):
        store.append(EventType.TRACK_CREATED, "tracking", payload={"thing": Opaque()})
    after = store.append(EventType.SYSTEM_STARTED, "system")

    assert after.seq == 2


def test_timestamps_come_from_the_injected_clock(clock: SimClock) -> None:
    """ADR 0003. Replayed events must carry the timestamps of the run being replayed, which
    is impossible if the store reads wall time."""
    store = SqliteEventStore(clock)

    first = store.append(EventType.SYSTEM_STARTED, "system")
    clock.advance(45.0)
    second = store.append(EventType.TRACK_CREATED, "tracking")

    assert first.timestamp == START
    assert second.timestamp == START + timedelta(seconds=45)
    store.close()


def test_empty_payload_defaults_to_empty_dict(store: SqliteEventStore) -> None:
    assert store.append(EventType.SYSTEM_STARTED, "system").payload == {}


# --------------------------------------------------------------------------------------
# Durability and persistence
# --------------------------------------------------------------------------------------


def test_events_survive_reopening(tmp_path: Path, clock: SimClock) -> None:
    """The property that makes backend restart recoverable: in-memory state is a projection
    of the log, so it can be rebuilt by replaying from seq 0 (FM-030)."""
    path = tmp_path / "events.db"

    store = SqliteEventStore(clock, path)
    store.append(EventType.TRACK_CREATED, "tracking", track_id="TRK-00001", payload={"x": 1})
    store.close()

    reopened = SqliteEventStore(clock, path)
    events = list(reopened.read_from(1))

    assert len(events) == 1
    assert events[0].track_id == "TRK-00001"
    assert events[0].payload == {"x": 1}
    reopened.close()


def test_durable_store_uses_wal(tmp_path: Path, clock: SimClock) -> None:
    store = SqliteEventStore(clock, tmp_path / "events.db")

    assert store.is_durable
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    store.close()


def test_in_memory_store_reports_itself_as_not_durable(store: SqliteEventStore) -> None:
    """In-memory has no durability and cannot use WAL, so it is only ever appropriate for
    tests. Recorded on the instance rather than left implicit."""
    assert not store.is_durable


def test_parent_directory_is_created(tmp_path: Path, clock: SimClock) -> None:
    store = SqliteEventStore(clock, tmp_path / "nested" / "deeper" / "events.db")

    store.append(EventType.SYSTEM_STARTED, "system")

    assert (tmp_path / "nested" / "deeper" / "events.db").exists()
    store.close()


def test_unopenable_path_raises_rather_than_degrading(tmp_path: Path, clock: SimClock) -> None:
    """FM-032. If events cannot be recorded the pipeline must halt, because continuing
    produces confident output that cannot afterwards be reconstructed or defended."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")

    with pytest.raises(EventStoreError, match="could not open"):
        SqliteEventStore(clock, blocker / "events.db")


def test_concurrent_reader_sees_committed_events(tmp_path: Path, clock: SimClock) -> None:
    """WAL's reason for being: readers alongside the single writer."""
    path = tmp_path / "events.db"
    writer = SqliteEventStore(clock, path)
    writer.append(EventType.TRACK_CREATED, "tracking", track_id="TRK-00001")

    reader = SqliteEventStore(clock, path)

    assert [e.track_id for e in reader.read_from(1)] == ["TRK-00001"]
    reader.close()
    writer.close()
