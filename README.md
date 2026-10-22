<p align="center">
  <img src="frontend/brand/aetos-logo-source.png" alt="Aetos" width="260">
</p>

# Aetos

**Command and control software for counter-drone teams.** Aetos takes raw
detections from drone sensors, turns them into tracked, classified,
risk-scored threats, and gives a human operator a clear recommendation
they can audit.

![The Aetos operator console tracking a simulated approach](docs/assets/console.png)

<sub>One drone, seen by three sensors, fused into one track. The amber
SIMULATION badge is set by the backend from the sensors actually attached,
not by the interface.</sub>

## Why it matters

Sites like airports, prisons and critical infrastructure now deal with
drones regularly. The sensors exist, but the hard part is the layer in
between: merging noisy detections from different sensors, deciding what is
a real threat, and showing an operator something they can act on with
confidence. That's what Aetos does.

Every judgement comes with its reasons, and every event is written to an
append-only log, so any decision can be replayed and explained afterwards.

## What it is, and what it isn't

Aetos is **software only, and it stops at a recommendation**. It has no
effector control, no weapons integration and no autonomous response. The
strongest thing it can output is `RESPONSE_RECOMMENDED`: advice to a human.
This is a deliberate, permanent boundary, not a missing feature.

## How it works

```
sensor → normalise → associate → track → classify → assess risk → recommend → operator
```

Each step is its own module with a defined interface. The tracker can't
tell a simulated detection from a real one: both arrive in the same format
through the same adapter, and a test enforces that. So everything you see
in simulation is the same code path a real sensor uses.

## Try it

```bash
./start.sh
```

Then open **http://localhost:8000**. One command sets up Python, builds the
interface and starts everything. Needs Python 3.11+ and Node 20+. On
Windows, run it from Git Bash.

```bash
./start.sh --list              # the nine built-in scenarios
./start.sh h_track_crossing    # run one
./start.sh --live              # a real radar instead of the simulator
```

By default all data is synthetic. Live mode works with a testbed radar
whose detection performance hasn't been measured yet, so it proves the
pipeline, not the sensor. See [docs/live_mode.md](docs/live_mode.md).

Tests: `pytest -q` for the backend, `cd frontend && npm test` for the
interface.

## Deploying

Aetos requires a login (viewer, operator and admin roles) and keeps an
audit trail of who did what. It refuses to start in an unsafe
configuration: it binds to localhost only, unless you put it behind a TLS
reverse proxy with `--behind-proxy`. Create the first account with:

```bash
.venv/bin/python -m backend.auth.admin add --username alice --role admin
```

See [docs/deployment.md](docs/deployment.md) for the full checklist, and
read [docs/failure_modes.md](docs/failure_modes.md) before relying on it in
the field.

## Repository

| Path        | What's there                                                                  |
| ----------- | ----------------------------------------------------------------------------- |
| `backend/`  | Ingestion, tracking, fusion, classification, zones, risk, decision, event log |
| `sensors/`  | The sensor adapter contract, the simulator and the testbed radar driver       |
| `frontend/` | The operator console                                                          |
| `twin/`     | A standalone 3D digital twin prototype, not yet wired to the backend          |
| `docs/`     | Architecture, schema, failure modes and design decisions (`docs/adr/`)        |

## Status

In active development. The roadmap (multi-sensor fusion at scale, a site
digital twin, distributed nodes, cross-site coordination) is in
[docs/vision.md](docs/vision.md). Current state of each module:
[docs/architecture.md](docs/architecture.md).
