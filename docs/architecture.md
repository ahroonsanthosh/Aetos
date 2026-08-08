# Aetos architecture

Living document. Updated as the system is built, not written afterwards.

## Scope boundary

Aetos is software only. It ends at a recommendation presented to a human operator. There is
no effector control, no weapons integration and no autonomous response execution anywhere in
this codebase, and none is planned for it. The most severe output the system can produce is
the recommendation category `RESPONSE_RECOMMENDED`, which is advice. Response and effector
integration is a separate future system with its own authorisation and safety architecture.

This boundary is architectural, not a phase-one simplification to be relaxed later.

## The governing principle

**The tracking engine cannot tell whether a detection came from the simulator or from a
physical sensor.**

Everything else in this document is subordinate to that. It is enforced by three things:

1. A single canonical `Detection` schema that every source must produce.
2. A single `SensorAdapter` interface that every source must implement.
3. A guard test (ADR 0002) that runs a scenario twice — once normally, once with
   simulator-privileged fields stripped — and requires identical track output.

The third is the one that actually holds the line. The first two are conventions.

## Pipeline

```
  ┌─────────────────────────────────────────────────────┐
  │  SOURCE   simulated adapter  │  (future) real adapter │
  └────────────────────┬────────────────────────────────┘
                       │  canonical Detection (WGS-84, UTC)
                       ▼
  ┌─────────────────────────────────────────────────────┐
  │  INGESTION   validate · deduplicate · normalise      │
  │              WGS-84 → local ENU metres               │
  └────────────────────┬────────────────────────────────┘
                       │
                       ▼
  ┌─────────────────────────────────────────────────────┐
  │  EVENT LOG   append-only · monotonic seq · durable    │◄── source of truth
  └────────────────────┬────────────────────────────────┘
                       │  everything downstream reads by seq
        ┌──────────────┼──────────────┬──────────────┐
        ▼              ▼              ▼              ▼
   TRACKING       WEBSOCKET       REPLAY         (future)
   association    broadcast       engine          audit export
   filtering
   lifecycle
   prediction
        │
        ▼
   FUSION  ── multi-sensor reconciliation
        │
        ▼
   CLASSIFICATION  ── what it is, and how confident (separate from where it is)
        │
        ▼
   RISK  ── score + reason codes + hysteresis
        │
        ▼
   DECISION  ── MONITOR │ VERIFY │ ALERT │ ESCALATE │ RESPONSE_RECOMMENDED
        │
        ▼
   OPERATOR (human)
        │
        ▼
   ── hard boundary ── (out of scope) authorised response
```

Each stage is a module with an explicit interface. They are never collapsed into a single
combined pipeline function.

## Why the event log sits where it does

Note that the event log is not off to one side as a logging concern. It is **in** the path.
Ingestion appends; tracking, the WebSocket broadcaster and the replay engine all read from
it by sequence number.

This is what makes replay trustworthy. Replay is not a second implementation that
approximates live behaviour — it is the same read path driven at a different rate. A bug
reproduced in replay is the same bug that happened live. See ADR 0006.

It also means in-memory state (active tracks, current alerts) is a *projection*, never an
independent authority. It can always be rebuilt by replaying from `seq` 0, which is what
makes backend restart recoverable.

## Coordinate frames and time

| Concern | Boundary | Interior |
|---|---|---|
| Position | WGS-84 lat/lon/alt at the schema and API edges | local ENU metres everywhere between (ADR 0004) |
| Altitude | metres above the WGS-84 ellipsoid, everywhere, no exceptions | — |
| Time | UTC ISO-8601 with milliseconds in the schema | injected `Clock`, never `datetime.now()` (ADR 0003) |
| Units | SI throughout: metres, metres per second, degrees for bearings only | — |

## Module map

| Module | Responsibility | Must not |
|---|---|---|
| `backend/common` | Clock, geodesy, identifiers, config loading | Import any other backend module (ADR 0009) |
| `backend/ingestion` | Validate, deduplicate, normalise, append to log | Make tracking decisions |
| `backend/events` | Append-only log, sequence ordering, replay reads | Interpret payload meaning |
| `backend/tracking` | Where things are: association, filtering, lifecycle, prediction | Decide what things are, or how dangerous |
| `backend/fusion` | Reconcile multi-sensor observations of one object | Re-run association |
| `backend/classification` | What it is and how confident | Estimate position or velocity |
| `backend/zones` | Site geometry, geofencing, zone-crossing prediction | Score risk |
| `backend/risk` | Score + reason codes + hysteresis | Recommend an action |
| `backend/decision` | Recommendation category | Execute anything, ever |
| `backend/system_health` | Watchdog, degraded mode, self-test | Suppress alerts |
| `backend/api` | REST + WebSocket | Hold authoritative state |
| `frontend` | Display and operator input | Be the source of truth for anything |

The "must not" column is the interface boundary. Tracking answers *where*. Classification
answers *what*. Risk answers *how dangerous*. Decision answers *what should a human consider
doing*. Keeping those four questions in four modules is what allows any of them to be
replaced — a learned classifier, a different filter, a different risk model — without
touching the others.

## Run modes

Three modes are designed for; only the first is built.

- **SIMULATION** — all detections synthetic. Current state.
- **LIVE** — all detections from real sensor adapters. Not built. No real sensor integration
  exists or is claimed.
- **HYBRID** — real and simulated detections coexisting, for controlled testing against a
  live baseline.

Because every source implements the same adapter interface and produces the same canonical
Detection, mode selection is adapter registry configuration rather than a code path. Nothing
downstream of ingestion is aware of which mode is running, except that sensor provenance is
recorded on every detection.

## Build order and current status

Per spec §20. Status updated as stages complete.

| # | Stage | Status |
|---|---|---|
| 0 | Foundation: ADRs, canonical schema, clock, geodesy, event store | in progress |
| 1 | Database + Track API skeleton | not started |
| 2 | Simulator producing synthetic detections | not started |
| 3 | Tracking: detections → tracks | not started |
| 4 | Map: tracks → visualisation (vertical slice complete) | not started |
| 5 | Track history | not started |
| 6 | Classification | not started |
| 7 | Risk scoring with reason codes | not started |
| 8 | Alerts | not started |
| 9 | Scenario library and replay | not started |
| 10 | Multi-sensor simulation and fusion | not started |
| 11 | Sensor adapter framework finalised, real stubs in place | not started |
| 12 | Chaos hardening, degraded-mode UI states | not started |

## Decision records

| ADR | Decision |
|---|---|
| [0001](adr/0001-language-and-stack.md) | Language and stack |
| [0002](adr/0002-detection-schema-provenance.md) | Detection provenance and the sim/real guard test |
| [0003](adr/0003-injectable-clock.md) | All time comes from an injected clock |
| [0004](adr/0004-enu-tracking-frame.md) | Tracking maths runs in a local ENU tangent plane |
| [0005](adr/0005-gnn-association.md) | Global Nearest Neighbour association with Mahalanobis gating |
| [0006](adr/0006-event-store-as-source-of-truth.md) | The event log is the source of truth |
| [0007](adr/0007-risk-hysteresis.md) | Risk hysteresis: dual thresholds plus minimum dwell |
| [0008](adr/0008-determinism-backpressure-auth.md) | Determinism, backpressure, authentication |
| [0009](adr/0009-repository-structure-deviations.md) | Repository structure deviations |
