# `backend/database` — persistence, and the projection of the log

## Responsibility

Durable storage for the things the system has to remember, behind repository interfaces so
the storage engine is a swap rather than a rewrite ([ADR 0001](../../docs/adr/0001-language-and-stack.md)).

It stores two categories of thing, and telling them apart is the point of this module.

| | Tables | Where the truth is |
|---|---|---|
| **Reference data** | `sites`, `sensors`, `scenarios` | Here. Configuration and inputs; nothing derives them, nothing can rebuild them. |
| **Projections** | `tracks`, `track_history` | **The event log.** These are a materialised read of it and nothing more. |

## The event log is the source of truth; track tables are a projection of it

This is [ADR 0006](../../docs/adr/0006-event-store-as-source-of-truth.md) and it is not a
slogan here, it is the mechanism.

`backend/database/projection.py` builds current track state by reading the log **by sequence
number**, exactly as the WebSocket broadcaster and any future replay engine do. It knows
nothing the log does not. Every row it writes came from an event, and the whole projection can
be discarded and reconstructed by replaying from `seq` 0:

```python
projection.rebuild()  # discard everything, replay the log from the beginning
```

That is what makes a backend restart recoverable rather than lossy (FM-030). It is also why
nothing else may write to the `tracks` table: a second writer that did not go through the log
would turn the projection from a view of what happened into a disagreeing account of it.

**The projection records the last `seq` it applied**, in `projection_state`. Three properties
follow, and each is tested:

- **Resumable** — a restarted backend reads on from its recorded position rather than
  replaying the whole log.
- **Idempotent** — the checkpoint is written *after* the rows, so a crash mid-batch resumes
  slightly behind and re-applies a bounded overlap. Each track row also carries the `seq` it
  last absorbed, so re-application is a genuine no-op rather than a rewrite with an older
  snapshot.
- **Halting** — an event the projection cannot apply raises rather than being skipped.
  Skipping would leave a projection that still answers requests but is quietly no longer a
  view of the log.

**The projection takes no clock.** Every timestamp it stores comes from the event that
produced it. If it stamped its own time, a rebuilt projection would differ from the original
run, and comparing the two — the reason to rebuild at all — would stop working.

## Track history is retained, never deleted

A track that goes LOST keeps its history permanently (spec §5). An object lost and later
reacquired is the case this exists for: without retained history the operator sees a "new"
object that is not new (FM-022).

The interface enforces it by omission — there is no method to delete one track's history. The
only deletion that exists is `discard_all()`, which empties a projection immediately before it
is rebuilt from the log.

## Storage

SQLite in WAL mode with `synchronous=FULL`, the same posture as the event store, for the same
reasons: single writer, concurrent readers, durable, no server process.

`FULL` is generous for the projection, which is rebuildable. It is kept because the same file
also holds sites, sensors and scenario definitions, which are not derivable from the log and
would be genuinely lost. Two durability postures in one file is a trap for whoever adds the
next table.

**The persistence database is a separate file from the event log, always.** Discarding and
rebuilding the projection is routine; it must be structurally incapable of touching the log it
is derived from. For the same reason, the projection tables carry none of the immutability
triggers the log does — those would be exactly wrong on data whose purpose is to be rebuilt.

Rows are stored as their model's JSON with query columns denormalised alongside, and
re-validated on read. A model change that would silently misread old rows raises
`RepositoryError` instead.

Passing no path gives an in-memory database with no durability and no WAL. Tests only; the
instance reports `is_durable == False` rather than leaving it implicit.

## Threading

One connection, opened with SQLite's default thread affinity, no lock — the same single-writer
posture as the event store. Repository calls must therefore happen on the thread that opened
the connection, which is why `backend/api`'s route handlers are `async def` and read inline
rather than being dispatched to a worker thread. See that module's README.

## Interface boundary

**Provides:** `SiteRepository`, `SensorRepository`, `ScenarioRepository`, `TrackRepository`,
`TrackHistoryRepository`, `ProjectionStateRepository`, and `TrackProjection`.

**Depends on:** `backend.common.clock` (all time is injected, ADR 0003),
`backend.events` (read-only — it reads the log by `seq` and never appends),
`backend.tracking.models`, `backend.zones.site`, `sensors.schemas.detection`.

**Does not:** own the `events` table. That belongs to `backend/events/store.py` and is not
reimplemented here. This module reads it.

**Does not:** decide anything about tracks. It records what the log says a track was. Which
detections belong to which track, when a track is confirmed and when it is lost are all
`backend/tracking`'s answers, arrived at before the event that this module reads.

## Contract this module currently asserts on another module's behalf

`backend/tracking` does not exist yet, so the payload shape of track lifecycle events has been
fixed here rather than there:

> A `TRACK_CREATED`, `TRACK_UPDATED`, `TRACK_CONFIRMED`, `TRACK_COASTING`, `TRACK_STALE` or
> `TRACK_LOST` event carries `payload["track"]`: the full `Track` as it stood **after** the
> change. `TRACK_CREATED` and `TRACK_UPDATED` must carry it; the others may omit it, and the
> projection then applies the state their type implies to the track it already holds.

Carrying the whole snapshot rather than a delta is deliberate. A projection that recomputed
track state from deltas would be a second implementation of tracking, and the two would drift
— which is the failure this architecture exists to prevent. When `backend/tracking` lands,
this contract should be restated there and referenced from here.

## Known gaps

- No migration mechanism. Schema changes currently surface as `RepositoryError` on read,
  which is loud but is not an upgrade path.
- No configuration loader populates `sites` or `sensors`; whatever composes the system does.
  Config loading belongs to `backend/common` (ADR 0009) and has not been built.
- Alerts have no projection, because no module produces alert events yet. When one does, an
  alert projection belongs here alongside the track one, with its own independent position in
  `projection_state`. Until then `GET /alerts` scans the log, which does not scale and says so.
