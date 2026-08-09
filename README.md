# Aetos — Counter-UAS Command & Control Platform

Aetos is a modular Counter-UAS (C-UAS) command-and-control platform. It ingests detections
from sensors, forms and maintains tracks, classifies them, assesses risk against a
configured protected site, and presents a human operator with a **recommendation**.

## Scope boundary — read this first

Aetos is **software only**. It stops at a human-facing recommendation.

- No effector control. No weapons integration. No autonomous response execution.
- The highest-severity output the system can produce is the recommendation category
  `RESPONSE_RECOMMENDED`, which is advice to a human operator and nothing more.
- This is a hard architectural boundary, not a phase-one simplification. Response and
  effector integration is a separate future system with its own authorisation and safety
  architecture, and does not belong in this codebase.

At this stage all detections are **synthetic**, produced by a built-in simulator. No
physical sensor integration exists or is claimed to exist.

## Core principle

The tracking engine cannot tell whether a detection came from the simulator or from a
physical sensor. Both produce the same canonical `Detection` through the same
`SensorAdapter` interface. This is enforced by test, not by convention — see
`docs/adr/0002-detection-schema-provenance.md`.

## Pipeline

```
sensor adapter → normalisation → association → track → classification
              → risk assessment → decision support → operator
```

Each arrow is a module boundary with an explicit interface. They are never collapsed into
a single combined pipeline function.

## Repository layout

| Path | Responsibility |
|---|---|
| `docs/` | Architecture, canonical schema, failure modes, decision records |
| `backend/common/` | Cross-cutting primitives: clock, geodesy, identifiers, config |
| `backend/ingestion/` | Detection validation and normalisation; event-bus entry point |
| `backend/tracking/` | Association, filtering, lifecycle, prediction |
| `backend/fusion/` | Multi-sensor reconciliation |
| `backend/classification/` | What the object is, and how confident |
| `backend/zones/` | Site geometry, geofencing, zone-crossing prediction |
| `backend/risk/` | Threat scoring with reason codes |
| `backend/decision/` | Recommendation category only — never execution |
| `backend/events/` | Append-only durable event store; replay |
| `backend/system_health/` | Watchdog, degraded-mode controller, self-test |
| `backend/database/` | Persistence adapters |
| `backend/api/` | REST + WebSocket |
| `sensors/` | `SensorAdapter` contract, simulated adapter, real-sensor stubs |
| `scenarios/` | Named, reusable scenario definitions |
| `frontend/` | Aetos operator interface — see its own README |
| `response/` | **Empty placeholder.** Future response integration; see its README |
| `config/` | Sites, sensor registry, environment configuration |
| `tests/` | Unit, integration, scenario replay, chaos |

## Running it

One command. It sets up Python, builds the interface, starts everything and opens a browser.

```bash
./start.sh
```

Then **http://localhost:8000** — a single process serves both the API and the operator
interface, so there is no second terminal and no proxy to configure.

```bash
./start.sh --list                  # show the nine scenarios and what each one is for
./start.sh h_track_crossing        # run a specific one
./start.sh --no-scenario           # idle system, no synthetic data
./start.sh --port 9000             # serve somewhere else
```

Needs Python 3.11+ and Node 20+. Re-running is cheap: it skips whatever is already done.

### Or with Docker, if you would rather not install either

```bash
docker compose up      # then http://localhost:8000
```

### Tests

```bash
pytest -q                      # backend
cd frontend && npm test        # interface
```

## Before you put this anywhere

**Aetos has no authentication of any kind.** No login, no roles, no authorisation checks.
Anyone who can reach the port has the full operator interface and the complete event log.
This is a deliberate, recorded decision — a half-built auth system is worse than none because
it looks like security — but it means the system must stay on a machine you control.
`start.sh` binds to localhost and the Docker port is published to `127.0.0.1` for that reason.

See [ADR 0008](docs/adr/0008-determinism-backpressure-auth.md) and `docs/failure_modes.md`
FM-001.

## Status

Under active construction. See `docs/architecture.md` for the current state of each module
and `docs/adr/` for why things are built the way they are.
