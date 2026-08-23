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

Detections come from a built-in simulator **or** from real hardware. One physical sensor is
integrated — a 24 GHz FMCW ranging module on a serial UART — and the interface, conformance
suite and adapter factory that admit any other are built (`docs/sensor_interface_specification.md`).

What is **not** established is what that sensor can actually see. Its declared range is a
datasheet figure quoted against a car-sized target; whether it detects a small airframe at all
is unmeasured, `coverage_validated` is false, and the conformance suite reports that as a gap
rather than a pass.

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
| `docs/` | Architecture, canonical schema, sensor interface specification, failure modes, decision records |
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
| `sensors/` | `SensorAdapter` contract, capability declaration, conformance suite, adapter factory, simulated adapter, one live hardware driver |
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

### Windows

`start.sh` is a bash script — run it from **Git Bash**, not PowerShell or cmd.exe. If you
cloned with `git clone`, you already have Git for Windows installed, which includes it:
right-click the repository folder and choose **Git Bash Here**, then run `./start.sh` exactly
as above. It handles the Windows-specific paths (`.venv\Scripts\`) and opens your default
browser itself.

There is no native PowerShell equivalent yet. If you'd rather not install or use Git Bash, say
so and one can be added — but treat that as untested until it has actually been run on
Windows, the same way the Docker path below is flagged.

### Or with Docker, if you would rather not install either

```bash
docker compose up      # then http://localhost:8000
```

### Tests

```bash
pytest -q                      # backend
cd frontend && npm test        # interface
```

## Connecting your own sensors

Aetos is hardware-agnostic, and that is meant as a checkable claim rather than a slogan. A
customer connects the radar, cameras, RF sensors and ADS-B receiver they already own; they do
not replace their infrastructure because they bought this.

The system adds no error of its own and removes none — every stage propagates the uncertainty it
was given rather than discarding it or replacing it with a convenient assumption. So the
operator picture is exactly as good as the sensors feeding it, better sensors improve it the
afternoon they are fitted, and the entry requirement is not accuracy but **honesty**:

> Aetos does not require a good sensor. It requires a sensor that tells the truth about how
> good it is.

An honest 15-metre error is usable. A source claiming two metres and delivering fifteen is not,
at any accuracy, because the filter believes it.

What a source has to do is set out in
**[docs/sensor_interface_specification.md](docs/sensor_interface_specification.md)** — three
floors, one grade, and every threshold derived from the tracker's own tuning rather than written
down. And it is a test result, not a claim:

```bash
python -m sensors.adapters.conformance                       # every sensor at the configured site
python -m sensors.adapters.conformance --sensor radar_north  # just one
```

The suite reports the floor each source reaches, every requirement it failed, and — deliberately
— every requirement it **could not check**. It establishes declared conformance and says so in
those words: no test can determine that a sensor claiming three metres delivers it, and that is
a measurement activity, not a software one.

### One real sensor is integrated

A 24 GHz FMCW ranging module on a serial UART, end to end: wire codec, transport, adapter,
link-integrity health and a hardware bench tool (`sensors/adapters/ld2451/`).

```bash
python -m sensors.adapters.ld2451.bench --list                 # find the port
python -m sensors.adapters.ld2451.bench --port /dev/ttyUSB0    # talk to the device
python -m backend.main --site bench --mode live --no-scenario  # run the system on it
```

`sensors/adapters/ld2451/BENCH.md` is the procedure, in order, from an unopened box to a
measured coverage figure. What that sensor can *actually see* is not established: its declared
range is a datasheet figure quoted against a car, `coverage_validated` is false, and the
conformance suite reports it as a gap rather than a pass. Closing it is a flight test, not a
software task.

LIVE and HYBRID still refuse to start with no real adapter attached, because every screen would
say LIVE over synthetic data.

## Before you put this anywhere

**Aetos requires a login, and refuses to start in a configuration that would expose it.**
Session cookies, three roles (`viewer` / `operator` / `admin`), scrypt password hashing, and
an audit trail that records who logged in, who acknowledged what, and when.

It does **not** terminate TLS. It binds loopback by default and refuses to bind anything else
without `--behind-proxy`, which is your assertion that a reverse proxy in front of it is
handling TLS. It also refuses to start unauthenticated off loopback, or with no accounts at
all. Create the first account before deploying:

```bash
# Use the interpreter inside .venv, not the bare `python` on PATH — the dependencies live
# there. On Windows the layout is Scripts, not bin.
.venv/bin/python -m backend.auth.admin add --username alice --role admin           # Linux, macOS
.venv/Scripts/python.exe -m backend.auth.admin add --username alice --role admin   # Windows
```

`./start.sh` prints the correct command for your machine if no account exists yet.

**[docs/deployment.md](docs/deployment.md)** has the proxy configuration and a post-deploy
checklist. [ADR 0016](docs/adr/0016-authentication-and-authorisation.md) explains the design;
`docs/failure_modes.md` FM-001 records what this replaced.

For a single-user machine, `--no-auth` restores the old behaviour — every action attributed to
one local operator with nothing checked. It works on loopback only, and `GET /system/health`
says so in as many words when it is in use.

**Read `docs/failure_modes.md` before relying on this.** Authentication is closed; sensor
silence, restart recovery and saturation are not, and they matter more on a site than on a
laptop.

## Status

Under active construction. See `docs/architecture.md` for the current state of each module
and `docs/adr/` for why things are built the way they are.
