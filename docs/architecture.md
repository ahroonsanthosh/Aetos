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
| `backend/fusion` | Reconcile multi-sensor observations of one object | Re-run association, or alter any track's estimate |
| `backend/classification` | What it is, how confident, and why | Estimate position or velocity, or hold track state |
| `backend/zones` | Site geometry, geofencing, zone-crossing prediction | Score risk |
| `backend/risk` | Score + confidence + reason codes + hysteresis; cycle-level situation findings | Recommend an action, or hold track state |
| `backend/decision` | Recommendation category | Score risk, or execute anything, ever |
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
| 0 | Foundation: ADRs, canonical schema, clock, geodesy, event store | **done** |
| 1 | Database + Track API skeleton | **done** |
| 2 | Simulator producing synthetic detections | **done** |
| 3 | Tracking: detections → tracks | **done** |
| 4 | Map: tracks → visualisation (vertical slice complete) | **done** |
| 5 | Track history | **done** — projected, served, and shown on the map and history page |
| 6 | Classification | **done** — weighted evidence pooling, distribution output, reason codes (ADR 0012) |
| 7 | Risk scoring with reason codes | **done** — ten factors, grouped combination, hysteresis, reason codes (ADR 0013) |
| 8 | Alerts | **done** — promotion, acknowledgement, escalation, rebuild from the log (ADR 0015) |
| 9 | Scenario library and replay | partial — all nine scenarios exist; replay engine not built |
| 10 | Multi-sensor simulation and fusion | **done** — track-level reconciliation into objects, with hysteresis and an extent ceiling (ADR 0017) |
| 11 | Sensor adapter framework finalised, real stubs in place | partial — interface done; real stubs not written |
| 12 | Chaos hardening, degraded-mode UI states | partial — sensor watchdog and degraded mode built (ADR 0018); chaos hardening not started |

The backend pipeline runs end to end today: a scenario drives the simulator, detections flow
through ingestion into tracking, classification assesses each track from pooled evidence, risk
scores it from ten factors and hysteresis turns that into a level, decision turns the level
into a recommendation for a human, alerting promotes the levels that warrant interrupting
someone into alerts an operator can acknowledge, everything is recorded in the event log and
projected into the database, and the API serves it to an interface that draws it.

Fusion sits between tracking and classification, so everything downstream counts objects rather
than observations: one drone seen by three sensors is classified once, scored once and alerted on
once (ADR 0017). A watchdog polls every adapter's health and reports degraded mode when the
picture thins (ADR 0018).

Two of those stages produce values they do not own. Classification and risk each write their
result back through the tracker, which holds it and carries it in the snapshot on every track
event, so the log rebuilds into the same state the live system holds. That inversion exists
because the alternative silently failed — see FM-025 and `backend/classification/README.md`.

Risk is the most recently built and the one worth reading the reasoning for before changing:
it combines its factors by three different rules depending on what kind of statement each one
is, and the single-rule version it replaced was wrong in two directions at once
(`backend/risk/README.md`, ADR 0013).

### Verified, as against built

`tests/integration/test_pipeline.py` runs the whole chain on real scenario files with real
sensor models, and carries the ADR 0002 provenance guard: the same scenario run with and
without the simulator's privileged fields produces identical track state. That test is the
reason the sim/real interchangeability claim is stated as fact rather than intent.

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
| [0010](adr/0010-radar-ppi-display.md) | Operator map is a site-centred PPI, not a geographic basemap |
| [0011](adr/0011-frontend-honest-rendering.md) | The interface renders only what the backend actually knows |
| [0012](adr/0012-classification-by-weighted-evidence.md) | Classification by weighted evidence; what provenance means |
| [0013](adr/0013-risk-by-escalating-factors.md) | Risk as combined escalating factors, discounted only by evidence of harmlessness |
| [0014](adr/0014-optional-basemap.md) | An optional self-hosted basemap beneath the PPI |
| [0015](adr/0015-alerting-as-promotion.md) | Alerting promotes risk levels; it does not assess |
| [0016](adr/0016-authentication-and-authorisation.md) | Session authentication, three roles, and a startup guard |
