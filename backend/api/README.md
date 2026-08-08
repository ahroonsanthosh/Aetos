# `backend/api` — REST and WebSocket

## Responsibility

The only surface the operator interface talks to. It reads state and it accepts operator
actions. It does not hold state, decide anything, or run anything.

## There is no authentication

**Aetos has no login, no roles and no authorisation checks.** Every action is attributed to a
single configured local operator, and that identity is *asserted*, not verified.

The consequence, stated plainly: **the event log does not currently prove who did something.**
Any claim that it constitutes an audit trail of operator action is not yet supportable.
**Aetos in this state must not be exposed to an untrusted network.**

This is a decision ([ADR 0008](../../docs/adr/0008-determinism-backpressure-auth.md), FM-001),
not an omission. A half-built authentication system is worse than none, because it looks like
security and invites the system to be exposed on the assumption that something is checking.

What does exist is the seam: `Identity`, `OperatorContext` and `IdentityProvider` in
`identity.py`. Every operator action carries an operator context, so actions are attributable
in structure from the first one ever recorded, and `authenticated=False` travels with the
identity so a future audit export cannot present an assumed identity as a verified one.

`IdentityProvider.resolve()` takes no request and reads nothing a client sends. There is no
header, cookie or body field by which a caller can name itself — a client's claim about who it
is, with nothing behind it, is not a weaker guarantee than the current one but a false one.

## The backend owns all authoritative state

Spec §14, and it shapes the whole module:

- **No PUT, PATCH or DELETE exists anywhere in this API.** Three POST endpoints exist: one
  stores a scenario definition, two report that they are not implemented.
- Server-assigned fields — identifiers, timestamps, the operator an action is attributed to —
  cannot be supplied by a client. Request models set `extra="forbid"`, so an attempt to send
  them is rejected rather than silently stripped; a request that was misunderstood should fail
  rather than appear to succeed.
- Judgements are computed once, here. `TrackView.displayable` is the backend's rule about what
  belongs on the map, sent as an answer rather than as inputs the frontend re-derives.
- The API holds no track table, no cached alert list, no "current" anything. Everything it
  answers with, it reads: from the event log by `seq`, or from a projection of that log. The
  only per-process state is each WebSocket client's cursor, which is a fact about the
  connection rather than about the world.

## The WebSocket stream reads the log; it is not fed by producers

`stream.py` pushes `TRACK_CREATED`, `TRACK_UPDATED`, `TRACK_LOST` and `ALERT_CREATED`
(spec §15), and the way it gets them is the point.

The tracker does not hand tracks to the broadcaster. Ingestion does not hand it detections. No
producer holds a reference to a connected client. A producer appends to the log; the log
notifies the broadcaster that *something* was committed; the broadcaster then reads the log by
sequence number — the same read path as the track projection and any future replay engine.

The commit notification carries the event, and the broadcaster **deliberately ignores it and
re-reads**. Using the handed-over object would be marginally faster and would make the live
stream a second code path that merely resembles replay. The two would drift, and a bug
reproduced in replay would stop being the bug that happened live, which is the only reason to
build replay ([ADR 0006](../../docs/adr/0006-event-store-as-source-of-truth.md)).

### Resuming without a gap

`seq` is a resume token.

```
GET /api/v1/tracks          → { tracks: [...], as_of_seq: 4192 }
WS  /api/v1/stream?from_seq=4193
```

The snapshot says exactly where the projection stood; the stream starts one past it. A client
that drops its connection reconnects with one past the highest `seq` it processed and
continues with neither a gap nor a duplicate. Without a published position the handover from
snapshot to stream is a race the client cannot win.

## Endpoints

| Method | Path | Status |
|---|---|---|
| GET | `/api/v1/tracks` | Real. Reads the track projection. |
| GET | `/api/v1/tracks/{id}` | Real. |
| GET | `/api/v1/tracks/{id}/history` | Real. Beyond spec §15; see below. |
| GET | `/api/v1/detections` | Real. **Debug and replay only — see below.** |
| GET | `/api/v1/alerts` | Real read of the log. Empty until a module produces alert events. |
| GET | `/api/v1/sensors` | Real. Placement only; no health field, because nothing computes health. |
| GET | `/api/v1/sites` | Real. |
| GET | `/api/v1/scenarios` | Real. Stored definitions. |
| POST | `/api/v1/scenarios` | Real. Stores a definition; storing is not running. |
| POST | `/api/v1/scenarios/{id}/start` | **501.** No scenario engine exists. |
| POST | `/api/v1/scenarios/{id}/stop` | **501.** |
| WS | `/api/v1/stream` | Real. |

Where a capability does not exist the endpoint says so. `POST /scenarios/{id}/start` returns
501 and **appends no event** — a `SCENARIO_STARTED` written by an endpoint that starts nothing
would make the log a record of something that did not happen, and the log's entire value is
that it is not that. `GET /alerts` is a genuine read of the log that currently finds nothing,
rather than a placeholder returning invented rows.

`GET /tracks/{id}/history` is beyond the spec §15 list. It is included because track history
is retained permanently after a track goes LOST (spec §5) and would otherwise be unreachable
over the API.

### `GET /detections` is not an operator surface

`docs/architecture.md` records this as a debug and replay endpoint. **The operator interface
displays tracks and never raw detections** (spec §3). Do not wire it to the map.

A detection is a single instantaneous observation with no continuity and no identity. A map of
detections shows clutter, false alarms and one object rendered as many, which trains an
operator to distrust the display. The endpoint exists so a developer can see what the sensors
actually reported.

## Versioning

The version is in the path (`/api/v1`). Frontend and backend ship separately, so a breaking
change must be able to coexist with the version it breaks rather than requiring a simultaneous
deployment.

Wire models in `schemas.py` are **separate types from the domain models**. `Track`, `Site` and
`Detection` are shapes the internals are free to change; the wire format is a contract with a
client released on its own schedule. The separation also lets the boundary do work the domain
models should not — notably that **ENU state is never exposed**: internally position is metres
in a site-local tangent plane (ADR 0004), and outside the backend it is WGS-84, because a
client receiving ENU metres would need the site reference point and would then own a
coordinate transform the backend already owns.

## Threading

Route handlers are `async def` and read from SQLite inline. The repository connection has
SQLite's default thread affinity (see `backend/database/README.md`), so a `def` handler
dispatched to a worker thread would touch the connection from the wrong one. At the V0 ceiling
these reads are far below the cost of a round trip. If that stops being true, the fix is a
connection per worker thread or PostgreSQL, both of which the repository interface permits.

## Interface boundary

**Provides:** `create_app(services)`, the wire models in `schemas.py`, the identity seam,
and `EventBroadcaster`.

**Depends on:** `backend.database` (repositories and the track projection), `backend.events`
(read-only, by `seq`), `backend.common.clock`, and the domain models it projects into view
models.

**Does not:** append to the event log. Appending belongs to the modules that decide something
happened. The API currently appends nothing at all; when operator actions such as alert
acknowledgement land, they will append through the same single-writer path as everything else.

**Does not:** hold authoritative state, run scenarios, or make tracking, classification or risk
decisions. It reports what those modules recorded.

## Scope

No endpoint in this API does anything in the physical world, and none ever will. Aetos ends at
a recommendation presented to a human operator.

## Known gaps

- No process entrypoint. Nothing yet decides which site to load or where the database lives;
  configuration loading belongs to `backend/common` (ADR 0009) and has not been built.
- No rate limiting, no request size limits, no CORS policy. All are network-exposure concerns,
  and the system is not to be network-exposed in this state (FM-001).
- `GET /alerts` and `GET /detections` scan the log per request and bound the *scan* rather than
  the result, reporting `next_seq` so a caller can page honestly. Alerts should get their own
  projection when the alerting module lands, for the same reason tracks have one.
