# `backend/events` — the append-only event log

## Responsibility

The record of everything that has happened, and the authority on the order it happened in.

This module is **in the pipeline path, not beside it.** It is not a logging concern. Ingestion
appends to it; tracking, the WebSocket broadcaster and the replay engine all read back from
it by sequence number.

## The four guarantees

From [ADR 0006](../../docs/adr/0006-event-store-as-source-of-truth.md). All four are
load-bearing and all four are tested.

1. **Single writer.** One append path, no concurrent one. Without this, "the order things
   happened" becomes a question with no answer and multi-sensor replay stops being
   deterministic.
2. **Monotonic `seq`.** Every event gets a strictly increasing integer, assigned by the
   writer. This — not `timestamp` — is the ordering authority: sensor clocks disagree
   (FM-012), but `seq` is assigned in one place and cannot be argued with.
3. **Write-before-read.** An event is durably committed before any consumer is notified.
   Subscribers are notified *by* the log, not by the producer.
4. **Immutable.** No update, no delete. Corrections are new events. Enforced by SQLite
   triggers as well as by the interface, so a module that reaches past this API still gets
   an error.

## Why replay comes almost free

Because the WebSocket broadcaster and the replay engine read the same log by `seq`, replay
is not a second implementation approximating live behaviour — it is **the same read path at
a different rate**. A bug reproduced in replay is the same bug that happened live, which is
the entire reason to build replay at all.

The same property makes backend restart recoverable: in-memory state (active tracks, current
alerts) is a *projection* of the log, never an independent authority, so it can be rebuilt by
replaying from `seq` 0.

## Interface boundary

**Provides:** `EventStore.append()`, `read_from()`, `read_range()`, `read_for_track()`,
`latest_seq()`, `subscribe()`.

**Depends on:** `backend.common.clock` (timestamps come from the injected clock, so replayed
events carry the timestamps of the run being replayed) and `backend.common.identifiers`.

**Does not:** interpret payload meaning. The store records that a `RISK_CHANGED` event
happened with a given payload; what a risk level *means* belongs to `backend/risk`. Keeping
the store ignorant of payload semantics is what lets event types be added without touching it.

## Failure behaviour

If events cannot be durably recorded, `EventStoreError` is raised rather than swallowed. The
correct response is to halt: continuing to track without an audit trail produces confident
output that cannot afterwards be reconstructed or defended. See `failure_modes.md` FM-032.

A *subscriber* failing is different — the event happened, and the log's account of it stands
regardless of who failed to process it. The failure is recorded as a `SUBSCRIBER_FAILED`
event so it is visible rather than silent, the remaining subscribers still run, and the
committed event is not rolled back.

## Storage

SQLite in WAL mode: single writer, concurrent readers, durable, no server process.
`synchronous=FULL`, because the write-before-read guarantee rests on the event actually
surviving process loss. Behind the `EventStore` interface, so PostgreSQL is a swap rather
than a rewrite.

Passing no path gives an in-memory log with no durability and no WAL. That is for tests only,
and the instance reports `is_durable == False` rather than leaving the distinction implicit.

## Known gap

The log grows without bound; no retention policy exists yet (FM-003). Any future retention
scheme must preserve immutability and completeness for whatever window it claims to cover,
rather than quietly truncating history.
