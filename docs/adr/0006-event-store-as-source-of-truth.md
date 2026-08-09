# ADR 0006 — The event log is the source of truth: single writer, monotonic sequence

- **Status:** Accepted
- **Date:** 2026-08-08

## Context

Spec §13 requires an append-only, durable event store, written *before* anything downstream
reads it, and treats it as the source of truth rather than a convenience log. Spec §18
requires full replay. Spec §14 requires that the backend owns all authoritative state.

Stated as a principle this is the strongest idea in the specification. Stated only as a
principle, it degenerates into a log file that drifts from what the system actually did, and
replay diverges from live behaviour in ways nobody can explain.

The requirement "written before anything downstream reads it" is a hard ordering guarantee
on the hot path. It needs a concrete mechanism.

## Decision

The event store is an append-only log with these properties:

1. **Single writer.** All appends go through one writer. No concurrent append path exists.
2. **Monotonic sequence number.** Every event receives a strictly increasing integer `seq`,
   assigned by the writer. `seq` is the total order of everything that has happened.
3. **Write-before-read.** An event is durably appended before any downstream consumer is
   notified of it. Consumers are notified *by* the log, not by the producer.
4. **Consumers read by seq.** The WebSocket broadcaster, the replay engine and any future
   audit export all read from the log by sequence position. They are the same code path.
5. **Immutable.** No update, no delete. Corrections are new events.
6. Storage is SQLite in WAL mode: single writer, concurrent readers, durable, no server
   process. Behind a repository interface so PostgreSQL is a swap.

Every event carries: `seq`, `event_id`, `timestamp` (from the injected clock, ADR 0003),
`event_type`, `source`, `track_id` where applicable, and a typed `payload`.

## Rationale

Point 4 is what makes the claim true rather than aspirational. If the live WebSocket stream
were fed directly from the tracker while replay were fed from the log, they would be two
implementations that drift. Feeding both from the log by `seq` means **replay is not a
separate feature** — it is the same read path at a different rate. A bug reproduced in
replay is the same bug that happened live, which is the entire point of building it.

Single writer with a monotonic sequence removes an entire class of ordering bugs. With
concurrent writers, "the order things happened" becomes a question with no answer, and
replay of a multi-sensor scenario stops being deterministic.

Immutability is what makes the log defensible for any future audit or insurer-facing export.
A log that can be amended is evidence of nothing.

## Consequences

- Durable append is on the critical path. At the V0 ceiling (tens of events per second)
  SQLite WAL handles this with substantial headroom. If throughput ever exceeds it, the fix
  is group commit — batching appends and fsyncing once per batch — which trades a bounded
  window of durability for throughput and requires a new ADR.
- The log grows without bound. Retention and archival policy is deferred, and is recorded as
  an open item in `docs/failure_modes.md`. Deleting is not the same as retention: any
  retention scheme must preserve the audit property for the window it claims to cover.
- In-memory state (active tracks, current alerts) is a **projection** of the log, not an
  independent authority. It can always be rebuilt by replaying from `seq` 0. This is what
  makes backend restart recoverable rather than lossy, and it is a chaos test.
