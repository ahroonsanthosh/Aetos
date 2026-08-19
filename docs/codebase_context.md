# Aetos — codebase context

**Purpose of this document.** A single orientation file for someone (or some session) picking
this repository up cold and intending to build on it. It covers what the system is, how it is
put together, what is genuinely finished versus aspirational, where the seams for new work are,
and what has recently been fixed and why. It is deliberately opinionated about the *reasons*,
because most of this codebase's decisions are only safe to change once you know what they were
protecting against.

Written 2026-08-19, against branch `claude/examine-codebase-sck2hv`.

---

## 1. What Aetos is

A modular **Counter-UAS command-and-control platform**. It ingests detections, forms and
maintains tracks, classifies them, scores risk against a configured protected site, and presents
a human operator with a **recommendation**.

### The scope boundary — read this before designing anything

Aetos is **software only, and it stops at a human-facing recommendation.**

- No effector control, no weapons integration, no autonomous response execution.
- The most severe output the system can produce is the recommendation category
  `RESPONSE_RECOMMENDED`, which is advice to a person and nothing else.
- This is an architectural boundary, not a phase-one simplification.

It is enforced, not merely documented: `tests/unit/test_module_boundaries.py` fails the build if
activation vocabulary (`effector`, `jammer`, `weapon`, `fire_control`, …) appears anywhere in
`backend/` or `sensors/`, and an API test asserts no route exposes anything acting on the
physical world. `response/` is an empty directory whose README explains what design work
(authorisation, safety interlocks, audit, legal review) must happen before any code goes there.

If you are adding a feature that touches this boundary, the correct first step is a new ADR, not
a first commit in `response/`.

### The governing principle

**The tracking engine cannot tell whether a detection came from the simulator or from physical
hardware.** Both produce the same canonical `Detection` through the same `SensorAdapter`
interface. This is enforced by test (`tests/integration/test_pipeline.py` runs a scenario with
and without the simulator's privileged fields and asserts identical track state), and it was
re-verified empirically during the last review pass: nothing in `backend/` branches on sensor
kind.

---

## 2. Layout

```
docs/            Architecture, canonical schema, failure modes, 16 ADRs
config/          Sites, zones, sensor registry, risk/auth/alerting/classification policy
backend/
  common/        Clock, geodesy, identifiers, config loading  (imports no other backend module)
  ingestion/     Dedupe, validate, record, reduce — the single entry point
  tracking/      Association (GNN), Kalman filtering, lifecycle
  fusion/        EMPTY — not built. See FM-023.
  classification/  Weighted evidence pooling → label distribution
  zones/         Site geometry, zone containment, time-to-boundary
  risk/          Factor scoring, grouped combination, hysteresis, situation correlation
  decision/      Risk level → recommendation. Small on purpose.
  events/        Append-only SQLite event log. The source of truth.
  system_health/ EMPTY — not built. Watchdog/degraded mode are unimplemented.
  database/      Repositories, SQLite adapters, the track projection
  api/           REST + WebSocket + auth routes + static serving
  auth/          Accounts, scrypt credentials, sessions, roles, admin CLI
sensors/
  schemas/       The canonical Detection schema
  adapters/      SensorAdapter contract; simulated adapter; uart_fmcw_radar (testbed hardware)
scenarios/       Nine named synthetic scenarios + trajectory/world model
frontend/        React + TypeScript operator interface (Vite)
response/        EMPTY placeholder. Do not put code here — read its README first.
tests/           unit, integration, scenario_replay, chaos
```

### Pipeline

```
sensor adapter → ingestion → association → track → classification
              → risk → decision → operator
```

Each arrow is a module boundary with an explicit interface. They are never collapsed into one
combined pipeline function. `backend/main.py::Pipeline.step()` is the only place they are
assembled, and the ordering there is load-bearing (risk runs after classification because
identity is one of its inputs).

---

## 3. The load-bearing architectural decisions

These are the ones that will bite you if you change them without reading the ADR.

| # | Decision | Why it matters when you extend |
|---|---|---|
| 0002 | Detection schema + provenance | The tracker must never consume speed/heading/classification. `IncomingDetection` has no field to carry them — the guarantee is structural, not contractual. |
| 0003 | Injectable clock | **No module in `backend/` or `sensors/` may call `datetime.now()` or `time.monotonic()`.** An AST test fails the build. Everything takes a `Clock`. This is what makes replay and deterministic tests possible. |
| 0004 | ENU tracking frame | All maths in a local tangent plane in metres. Degrees are not a metric frame. |
| 0005 | GNN association | Global assignment over Mahalanobis distance, not greedy nearest neighbour. Greedy fails *by construction* at a track crossing. |
| 0006 | Event store is the source of truth | Every track event carries a **full track snapshot**, not a delta. The projection, the WebSocket and replay are all the same read path over `seq`. |
| 0007 | Risk hysteresis | Dual rise/fall thresholds + asymmetric dwell. The score is never smoothed; hysteresis applies to the discrete level only. |
| 0008 | Determinism, backpressure, auth | Identifiers are deterministic and sequential so a replay reproduces them. Seeded RNG per sensor. |
| 0012 | Classification by weighted evidence | Linear pooling, not a product of likelihoods, so one confident-and-wrong source cannot veto everything. |
| 0013 | Risk by escalating factors | Three combination rules, not one. See §5. |
| 0016 | Authentication and authorisation | Sessions, three roles, scrypt. Gate applied at router level so new routes are gated by default. |

---

## 4. What is actually finished vs. aspirational

**Be careful here — the repository is honest about this, and you should stay honest about it.**

### Built and working
- Ingestion, tracking (association/filter/lifecycle), classification, zones, risk, decision
- Event store, track projection, replay-from-log
- REST + WebSocket API, authentication/authorisation, audit trail
- Operator interface (map/PPI, track list, assessment panel, alerts)
- Nine scenarios, simulated sensors with honest degradation

### Empty or unimplemented
- **`backend/fusion/`** — nothing. This is the biggest functional gap; see §7.
- **`backend/system_health/`** — nothing. No watchdog, no degraded-mode controller, no sensor
  silence detection. `FM-010` (a silent sensor looking like an empty sky) is **open**.
- **`response/`** — deliberately empty, gated behind a design pass.
- **Scenario control API** — `POST /scenarios/{id}/start` returns "not implemented" honestly.

### Docker: present, does not start

`Dockerfile` and `docker-compose.yml` are at the repository root (they were under `frontend/`
with a build context that could not resolve their own `COPY` paths — fixed). The image is
sound. **`docker compose up` still does not start**, for a real reason:

The container must bind `0.0.0.0` to be reachable, and `guard_bind` refuses anything but
loopback without `--behind-proxy`, which asserts TLS termination in front of the process.
Compose publishes to `127.0.0.1` only, so the posture is genuinely safe — the process just
cannot verify it, and passing `--behind-proxy` would encode a claim about TLS that is false.

How a container declares its exposure is an open design decision. Do not paper over it by
passing `--behind-proxy`; that is a lie in a config file about a security boundary.

### Config that looks live but is not
`config/environments/{sim,staging,prod}.json` each declare a `risk.hysteresis` block **that
nothing reads.** The live risk tuning is `config/risk/policy.json`, in different units, with
different values. Nothing at runtime constructs an `EnvironmentConfig` at all. This is
documented in place now (the model docstring and both READMEs say so), but it has not been
reconciled — that is a decision about which units the deployment surface should speak, and it is
still open. **Tune `config/risk/policy.json`.**

`config/environments/*.json` also declares `queues` (backpressure capacities per ADR 0008) which
is likewise unread, because the queueing/shedding machinery lives in `system_health`, which is
empty.

---

## 5. The risk grading system

This is the part with the most design in it, and the part most worth understanding before
changing.

### Factors

Ten factors, each answering one question from one kind of information, each returning an
activation in `[0,1]` and a sentence describing what it observed.

| Factor | Group | Reads |
|---|---|---|
| `zone_incursion` | engagement | Occupied zone, weighted by configurable per-kind activation |
| `closing_on_protected_area` | engagement | Time-to-boundary on the current course |
| `proximity_to_protected_area` | engagement | Distance, log-scaled |
| `closing_velocity` | engagement | Radial speed toward the asset |
| `direct_approach` | engagement | Angle between course and bearing to the asset |
| `loitering_near_asset` | behaviour | Path length vs net displacement, near the asset |
| `low_altitude_near_asset` | behaviour | Altitude, gated on proximity |
| `identified_as_uas` | corroborating | Classification mass on UAS labels × confidence |
| `persistent_track` | corroborating | Detection count and track age |
| `multi_sensor_corroboration` | corroborating | Distinct sensors that have held the track |

### Combination — three rules, not one

- **Engagement: weighted maximum.** These are five views of *one* situation, not five
  independent concerns. Noisy-OR counted the same fact five times and pinned ordinary inbound
  traffic at 100.
- **Behaviour: noisy-OR with engagement.** Loitering and low-altitude approach genuinely are
  independent of the trajectory picture.
- **Corroborating: scales the base, cannot create it.**
  `base + (1 − base) · base · corroboration`. At zero base it does exactly nothing.

Then `score = 100 · escalation · mitigation`.

### The safety property

**Absence of evidence never reduces risk.** Only a positive, confident identification of
something harmless may lower a score, and never to zero (floor `min_mitigation`, default 0.35).
`unknown` is deliberately absent from `BENIGN_LABELS`, and that absence is the safety property
of the whole module. An unknown contact behaving like a threat scores like a threat.

### Score and confidence are separate

Confidence is **never** multiplied into the score. A thin-evidence track on alarming geometry is
exactly the case an operator must see; folding them together would quietly suppress it. The
decision layer downgrades thin-evidence escalations to `VERIFY` rather than hiding them.

### Two test files, two different jobs

- `tests/unit/test_risk.py` — the safety properties.
- `tests/unit/test_risk_calibration.py` — **the shape of the curves, all the way along.**

That split was earned. The time-to-boundary ramp shipped with its exponent inverted and every
test passed, because the endpoints were right and only the middle was wrong. Calibration is
asserted as monotonicity and mid-curve relationships, never exact scores — a test pinning an
exact number to a tuning constant fails on every legitimate retune and teaches whoever hits it
to update the expected value without reading why.

### Known calibration observation (not a bug, worth knowing)

`closing_velocity` saturates at 25 m/s (weight 0.45), so any contact flying straight in at
≥25 m/s floors at MEDIUM regardless of range. That is the configured design, but it is the first
knob to look at if mid-range grading feels hot.

---

## 6. Integrating the testbed radar (HLK-LD2451)

You have a Hi-Link HLK-LD2451 (24 GHz FMCW, vehicle firmware, UART) plus a CP2102 USB-TTL
bridge. **The adapter is written, tested, and not yet wired in.** That wiring is the next
integration step.

### What exists

`sensors/adapters/uart_fmcw_radar/` — a complete, isolated, swappable `SensorAdapter`:

| File | Responsibility |
|---|---|
| `protocol.py` | Pure frame parsing. No I/O, no `pyserial` import. Testable with byte literals. |
| `transport.py` | The only file touching a port. `pyserial` lazy-imported. |
| `config.py` | `SerialLinkConfig` (the wire) + `RadarMountConfig` (geometry, calibration) |
| `adapter.py` | `UartFmcwRadarAdapter` → canonical `Detection` |

Install the optional dependency with `pip install .[hardware]`.

### Verified integration readiness

Confirmed empirically during the final review: a `SensorType.RADAR` detection stream flows
through ingestion → tracking → classification → risk → recommendation and produces sensible
grading, with **nothing downstream branching on sensor kind**. Single sensor produces one clean
track (no multi-sensor fragmentation — see §7).

### What still needs deciding before wiring it in

The adapter is not in `backend/main.py::Pipeline` because `Site`/`SensorPlacement` assume a
`registry_key` into the *simulated* sensor registry, whose schema describes a simulator's noise
model (probability of detection, false-alarm rate) — not a real unit's serial port and mount
geometry. Folding a real adapter's config into that shape is a site-configuration design
decision, and it should be made deliberately.

Suggested shape (not implemented): let a `SensorPlacement` optionally name an adapter kind plus
an adapter-specific config block, so `Pipeline.attach_scenario` builds simulated adapters and a
new `Pipeline.attach_sensors` builds real ones, both producing `SensorAdapter`.

### Honest limits of this hardware — do not paper over these

- **Vehicle-tuned RCS, unverified for small airframes.** The firmware is tuned for cars. Whether
  a small UAS returns enough signal at useful range is untested. Treat detections as *what the
  module reported*, not as proof it can see a drone.
- **No native elevation.** Height is inferred entirely from a fixed `tilt_deg` mount angle.
  `elevation_uncertainty_deg` defaults deliberately wide to reflect that.
- **No hardware timestamp.** Frames are timestamped at receipt from the injected clock.
- **Direction/angle sign conventions unverified.** The wire format was reverse-derived from a
  community reference implementation, not the vendor PDF. Framing is solid (round-trips in
  tests); the *semantics* need a field check — walk a target toward, then away from, the mount
  and confirm `radial_velocity_mps` signs. There is a config flag to flip it.
- **Confidence is a relative proxy**, not a calibrated probability.

Before this is more than a testbed: run a calibration pass against known ranges and bearings and
replace the placeholder sigmas in `RadarMountConfig` with measured ones.

### AERIS-10, the other radar discussed

An open-source 10.5 GHz phased-array radar (hardware + FPGA firmware, CERN-OHL-P / MIT). It does
CFAR/MTI/Doppler on-board and geolocates detections with GPS/IMU, so it would slot in as a
second `SensorAdapter` more readily than raw SDR. It is Alpha, it is a serious PCB/RF build, and
it **transmits** — spectrum licensing and aviation-safety clearance are real prerequisites, not
formalities.

---

## 7. The biggest functional gap: no sensor fusion (FM-023)

GNN association is **1:1** — `linear_sum_assignment` matches at most one detection per track per
cycle. So one physical drone observed by four sensors produces up to four competing tracks, each
picking up a different sensor's detection each cycle.

Observed live during the final review, running scenario `b_approaching_threat` (which declares
**one** object):

```
tracks: 3 confirmed, all "medium_uas", all closing ~25 m/s
situation: "3 tracks appear to be 2 objects — 1 set(s) are co-located and co-moving"
```

Three tracks for one drone; the correlation clustering merged two of them and left one out.

### The mitigation that exists today

`backend/risk/correlation.py` clusters tracks that are **co-located and co-moving** (single-link
connected components; thresholds in `SituationPolicy`) and reports the gap to the operator —
*"8 tracks · likely 5 objects"* — rather than hiding it. This is explicitly **not fusion**: it
merges nothing, changes no track state, and its output is scoped to the situation finding.

### What proper fusion would change

`backend/fusion/` is empty and reserved. When it exists, the clustering in `correlation.py` is
deleted and the object count comes from fusion properly. This is probably the highest-value next
feature, and it directly affects grading accuracy: today, three fragments of one drone each get
scored, each get corroboration credit, and each can raise an alert.

**Note for the radar integration:** a single radar has no multi-sensor fragmentation. The
testbed path is cleaner than the four-sensor simulator, and `multi_sensor_corroboration` will
correctly stay silent.

---

## 8. Recent bug-fix history (this branch)

Every fix below was reproduced before being made and carries a regression test verified to fail
against the old code. Useful context because several reveal *classes* of bug worth watching for.

| Fix | What was wrong | Class |
|---|---|---|
| Clutter recorded as CONFIRMED | Demotion walked the *promotion* order to reach LOST, so an unsupported track emitted `TRACK_CONFIRMED` + `TRACK_STALE` on its way out — with one detection behind it. The log is the source of truth, so the fabrication reached the projection and operator history. | Asymmetric state machines |
| Inverted urgency curve | `closing_on_protected_area` raised its ramp to the power **0.5** instead of **2** — exactly its documented shape backwards. Contacts 1.5–3 min out graded HIGH. | Docstring/code divergence |
| Event store wedged permanently | A transient write failure spent an `event_id` with no row behind it; numbering resumed from the *row count*, re-minting an existing ID, and **every append failed forever after restart**. | Resume-point arithmetic |
| Order-dependent clustering | A track joined only the first cluster it matched, so the documented A–B–C chain survived only if B was processed in the middle. Same fragments → 1 or 2 objects depending on acquisition order. | Incremental clustering without merge |
| Zero uncertainty crashed the pipeline | Schema admits `position_uncertainty_m = 0.0`; ingestion built the covariance inline, bypassing the guard; the association gate's solve raised `LinAlgError` and took everything down. | Validation bypass |
| Revoked session kept its WebSocket | The stream authenticated once at upgrade and never again. Disabling an account returned 401 to REST while the socket kept delivering the whole live picture. | Long-lived connection auth |
| Radar adapter link recovery | Explicit `start()` honoured a stale reconnect backoff; DEGRADED state was unreachable; stale buffer survived a reconnect. | Reconnect state machines |
| Non-deterministic benign label | `BENIGN_LABELS` was a `frozenset`; a tie resolved by enum hash order, i.e. `PYTHONHASHSEED`. | Set iteration in deterministic paths |
| Expired sessions never purged | `purge_expired_sessions()` was correct and had zero callers. | Dead-but-correct code |

### Patterns worth carrying forward

1. **Endpoint tests miss curve bugs.** Assert monotonicity and mid-range relationships.
2. **Docstrings here are load-bearing.** Several bugs were found by reading code *against its
   own stated reasoning*. When they disagree, one of them is a bug — decide which.
3. **Resume points must derive from what was actually stored**, never from a count of it.
4. **Anything long-lived needs periodic re-authorisation**, not just an entry check.
5. `grep` for correct-but-uncalled functions.

---

## 9. Running, testing, and the quality gate

```bash
./start.sh                      # sets up Python, builds the UI, serves both on :8000
./start.sh --list               # the nine scenarios
./start.sh h_track_crossing     # run a specific one
./start.sh --no-scenario        # idle system
python -m backend.main --scenario b_approaching_threat --port 8321 --no-auth   # backend only
```

Needs Python 3.11+ and Node 20+. On Windows run `start.sh` from **Git Bash**, not PowerShell.

### Before deploying anywhere

Aetos **requires a login and refuses to start in a configuration that would expose it.** It does
not terminate TLS; it binds loopback by default and refuses anything else without
`--behind-proxy`. Verified working: it refuses no-auth off loopback, refuses zero accounts off
loopback, and refuses off-loopback without the proxy assertion.

```bash
.venv/bin/python -m backend.auth.admin --data-dir <dir> add --username alice --role admin
```

Note `--data-dir` is a **top-level** argument and must precede the subcommand. Password is
prompted, not passed as a flag.

### Full gate (all green as of this writing)

```bash
pytest -q                       # 557 passed
ruff check .                    # clean
cd frontend && npx vitest run   # 54 passed
cd frontend && npx tsc --noEmit  # clean
cd frontend && npm run build    # builds
```

---

## 9a. What was verified by running it, not just by testing it

The final review pass exercised the system rather than only its test suite. Recorded here so a
later session knows what is genuinely proven versus merely asserted.

- **All nine scenarios, each for its full declared duration** (200–360 s). Grading discriminates
  correctly: `a_benign_transit` peaks MEDIUM with **zero alerts** and never gets closer than
  1784 m; `b_approaching_threat` reaches CRITICAL, raises 6 alerts, and penetrates to −791 m
  inside the protected area. Every other threat scenario reaches CRITICAL and alerts.
  *(Beware: a truncated run makes benign and threat scenarios look identical — the threats
  simply have not arrived yet. Run full durations.)*
- **`./start.sh` from cold** with `.venv` and `frontend/dist` deleted: rebuilds and serves in
  ~18 s.
- **The operator interface rendered against live data** in Chromium: PPI display, zone rings,
  track markers, track table, honest "not assessed" on unassessed tracks, `FUSION NOT BUILT`
  chip. **Zero console errors, zero failed requests.** SPA deep-link (`/health`) returns 200 on
  a hard refresh.
- **Deployment guard** refuses all three unsafe binds and permits loopback development.
- **Auth end to end**: admin CLI → login → gated routes 401→200, OpenAPI gated, wrong password
  refused, full audit trail written.
- **A real `SensorType.RADAR` stream** through ingestion → tracking → classification → risk to a
  sensible recommendation, with nothing downstream branching on sensor kind.
- **Not verified**: the Docker path (no daemon available, and it does not start anyway — above).

---

## 10. Where to start on new work

Roughly in order of value:

1. **`backend/fusion/`** — the biggest accuracy gap (§7). Everything else is cleaner once one
   object is one track.
2. **`backend/system_health/`** — the watchdog. FM-010 (a silent sensor is indistinguishable
   from an empty sky) is open and matters more on a site than on a laptop. `SensorAdapter.health()`
   already reports the facts; nothing consumes them.
3. **Wire in the testbed radar** (§6) — needs the site-configuration decision first.
4. **Reconcile the two hysteresis configs** (§4) — pick which units the deployment surface speaks.
5. **Decide how a container declares its exposure** (§4, Docker) — small, and it unblocks
   `docker compose up`.
6. **Restart recovery and saturation** — see `docs/failure_modes.md`; both open.

Read `docs/failure_modes.md` before relying on any of this. Authentication (FM-001) is closed;
sensor silence, restart recovery and saturation are not.

---

## 11. Conventions that will trip you up

- **Never call `datetime.now()` or `time.monotonic()`** in `backend/` or `sensors/`. Take a
  `Clock`. A test enforces it.
- **No vendor or model names** in `backend/` or `sensors/` source. A parametrised test enforces
  it. Hardware specifics live in an adapter's README, in prose.
- **No activation vocabulary** anywhere. A test enforces it.
- **Config is strict** (`extra="forbid"`), so a typo in a factor name is a startup error rather
  than silently ignored tuning.
- **Events are immutable** — SQLite triggers reject UPDATE and DELETE on the events table, not
  just the Python interface.
- **The tracker owns track state.** Classification and risk compute values and hand them back
  through `apply_classification` / `apply_threat`. One owner per piece of state is what makes an
  event snapshot the whole truth (FM-025).
- **Docstrings carry the reasoning.** If you change behaviour, change the docstring in the same
  commit; several real bugs here were divergences between the two.
