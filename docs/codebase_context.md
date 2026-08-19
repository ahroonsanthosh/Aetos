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
docs/            Architecture, canonical schema, failure modes, 18 ADRs
config/          Sites, zones, sensor registry, risk/auth/alerting/classification policy
backend/
  common/        Clock, geodesy, identifiers, config loading  (imports no other backend module)
  ingestion/     Dedupe, validate, record, reduce — the single entry point
  tracking/      Association (GNN), Kalman filtering, lifecycle
  fusion/        Track-level reconciliation: which tracks are one object (ADR 0017)
  classification/  Weighted evidence pooling → label distribution
  zones/         Site geometry, zone containment, time-to-boundary
  risk/          Factor scoring, grouped combination, hysteresis, situation correlation
  decision/      Risk level → recommendation. Small on purpose.
  events/        Append-only SQLite event log. The source of truth.
  system_health/ Sensor watchdog, corroborated silence, degraded mode (ADR 0018)
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
sensor adapter → ingestion → association → track → fusion → classification
              → risk → alerting → watchdog → operator
```

Each arrow is a module boundary with an explicit interface. They are never collapsed into one
combined pipeline function. `backend/main.py::Pipeline.step()` is the only place they are
assembled, and the ordering there is load-bearing:

- **Fusion runs before classification**, so everything downstream counts objects rather than
  observations. Classifying and scoring three fragments of one drone, then reconciling them,
  would mean three of everything had already happened.
- **Risk runs after classification**, because identity is one of its inputs.
- **The watchdog runs last**, after the adapters have been polled for the cycle. A watchdog that
  sampled mid-cycle would report a gap it happened to look through.

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
| 0017 | Fusion on tracks, not detections | Reconciliation may not merge estimates or re-run association. Over-merging hides a contact; under-merging is only untidy. Thresholds are asymmetric because the failures are. |
| 0018 | Silence judged against other sensors | A sensor alone in a quiet sky is indistinguishable from a broken one. Thresholds are declared, never derived from update rate. |

---

## 4. What is actually finished vs. aspirational

**Be careful here — the repository is honest about this, and you should stay honest about it.**

### Built and working
- Ingestion, tracking (association/filter/lifecycle), **fusion**, classification, zones, risk,
  decision, alerting, **the sensor watchdog**
- Event store, track projection, replay-from-log
- REST + WebSocket API, authentication/authorisation, audit trail
- Operator interface (map/PPI, track list, assessment panel, alerts, sensor conditions)
- Nine scenarios, simulated sensors with honest degradation — including scripted availability
  windows, which until recently were dead configuration nothing applied

### Empty or unimplemented
- **`response/`** — deliberately empty, gated behind a design pass.
- **Scenario control API** — `POST /scenarios/{id}/start` returns "not implemented" honestly.
- **Backpressure/queue shedding** (ADR 0008) — `config/environments/*.json` declares `queues`
  and nothing reads it. This was previously blamed on `system_health` being empty; that module
  exists now, so the reason is gone and the work simply has not been done.
- **Estimate-level fusion** — fusion *chooses* a presented track rather than combining
  estimates, deliberately (§7). Combining them is a real next step and needs its own ADR.

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

`config/environments/*.json` also declares `queues` (backpressure capacities per ADR 0008),
likewise unread. `config/fusion/policy.json` and `config/system_health/policy.json` **are** read,
by the modules of those names.

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
through ingestion → tracking → fusion → classification → risk → recommendation and produces
sensible grading, with **nothing downstream branching on sensor kind**. A single sensor produces
one clean track, so fusion has nothing to reconcile and says so honestly rather than staying
silent (§7).

**The watchdog on a single-sensor site.** Silence is only anomalous when *other* sensors are
producing, so a site with one radar can never call that radar silent — and the reason text says
that, rather than implying a check that is not happening. That is a real limit of the testbed
configuration, not a bug to fix by lowering the bar. See §7.

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

## 7. Fusion and the watchdog

Both modules were empty at the last handover and both are now built. They are described together
because they were built together and share a discipline: each answers a question the rest of the
system genuinely could not, and each refuses to answer a neighbouring question it cannot.

### 7a. Fusion — `backend/fusion/` (ADR 0017, closes FM-023)

**The problem.** GNN association is 1:1 — `linear_sum_assignment` matches at most one detection
per track per cycle, which is correct and is what scenario H exists to prove. So one drone seen
by three sensors delivers three detections, two go unassigned, and unassigned detections start
new tracks. Every stage behaves as designed and an operator counts three contacts. Observed, not
theorised: `b_approaching_threat` declares **one** object and produced three CONFIRMED tracks.

**The placement.** Between tracking and classification, operating on tracks. Loosening the
association gate was rejected in FM-023's own text — it would merge genuinely distinct objects,
which is far worse. Merging detections upstream was rejected as the larger change to the riskier
place.

**The safety property, which drives every threshold.** *Merging two distinct objects is worse
than showing one object twice.* Over-merging hides a contact and the one that vanished is the one
nobody is watching. So separation is permissive (150 m — two radars observing one drone at 4 km
routinely disagree by that much) and velocity is strict (6 m/s), because two aircraft can pass
within 150 m but not while also matching course and speed.

**What it will not do**, and these are deliberate:
- Not re-run association.
- **Not merge track state.** The presented track is *chosen* from the members, so every
  coordinate on screen is one the tracker actually produced. A covariance-weighted mean is
  statistically better and is still a position no sensor reported.
- Not retire anything. Members stay tracked and stay in the log;
  `GET /tracks?include_supporting=true` returns them.

**Two defects that measurement found and reasoning did not.** Worth knowing, because both are
the kind that a green test suite will not show you:

1. **Identity had to be resolved globally.** When a group splits — constantly, as a fragment
   drifts outside the gate — both halves carried the old object id and both inherited it. The
   incumbent-primary record was then overwritten by whichever group was processed last and the
   presented track flipped every cycle; alerts key on the presented track, so each flip cleared
   one alert and raised another. Measured: **320 of 640 cycles held a duplicated id, 708 primary
   flips.** A contested id now goes to exactly one group.
2. **Grouping needed hysteresis and an extent ceiling.** A single threshold made the grouping
   chatter and one drone produced **34 alerts against the unfused pipeline's 3**. Merge and split
   now have separate thresholds. Separately, single-link grouping chains: 400 tracks 10 m apart
   collapsed into one object spanning 4 km, and `i_swarm_saturation` produced a genuine object
   1497 m across. `max_object_extent_m` caps it hard.

With both in place, **alert volume fell below the unfused baseline on every scenario** — 3 → 1 on
`b_approaching_threat`, 54 → 28 on `i_swarm_saturation` — and object counts matched the declared
truth of all nine.

**Consequence: `backend/risk/correlation.py` no longer clusters.** Its clustering was always
described as a stand-in to delete once fusion existed, and leaving it would have been worse than
redundant — fusion holds a group together across a hysteresis band, so two objects it
deliberately keeps apart can sit closer than a single-threshold test would merge them. Two
subsystems answering "are these the same object" differently, in front of one operator, is worse
than either answer alone.

### 7b. The watchdog — `backend/system_health/` (ADR 0018, closes FM-010)

**The ambiguity at the centre of it.** A sensor producing nothing may be broken, or may be
looking at an empty sky. One sensor watched alone cannot distinguish those, and both guesses are
expensive: guess broken and it cries wolf through every quiet minute until the indicator is
ignored; guess empty sky and it stays silent through exactly the event it exists to catch.

**The decision: silence is anomalous only when other sensors are producing.** That is not a
heuristic standing in for something better — it is the actual discriminating information
available. It degrades honestly: a single-sensor site can never call its sensor silent, and the
reason text says so rather than implying a check that is not happening.

**Thresholds are declared, not derived from update rate.** The first implementation derived them
— eight missed update periods — and it was wrong, measurably. Update rate is how often the
adapter polls; it is not how often a target is in coverage. Measured across all nine scenarios
with no fault present anywhere:

| Sensor | Longest *legitimate* quiet spell | Derived threshold |
|---|---|---|
| `eo_ir_east` (narrow field, 10 Hz) | **81 s** | 5 s |
| `radar_north` (wide, 4 Hz) | 10.5 s | 5 s |
| `rf_central` (wide, 1 Hz) | 8 s | 8 s |

The healthiest sensor on the site was flagged continuously. Thresholds are now resolved
most-specific-first: sensor id, then sensor type, then site default.

**It must not suppress alerts.** A degraded site is a reason to tell an operator more, never
less. A system that quietened down as its sensing failed would be at its most reassuring when it
was least able to see.

**It made scripted sensor availability actually happen.** `apply_degradation` existed on the
simulated adapter and **nothing called it**, and `e_sensor_degradation` named a sensor id no site
defines — so the one scenario written to exercise sensor failure exercised nothing and said
nothing about it. Two further scenarios had the same dangling references. The pipeline now
applies availability windows every cycle, recomputed from elapsed scenario time so a window
cannot be missed between cycles and a replay is identical (ADR 0008), and refuses at attach time
to run a scenario scripting a sensor the site does not have.

### 7c. What they changed elsewhere

| Surface | Change |
|---|---|
| `GET /tracks` | One entry per object by default. `?include_supporting=true` returns every track. |
| `GET /tracks` response | Each track carries a `fusion` block: object id, primary flag, member roster, corroborating sensors. |
| `GET /sensors` | Each sensor carries a `condition` block from the watchdog — or `null`, meaning **nothing assessed it**, which is not the same as healthy. |
| `GET /system/health` | `fusion` and `system_health` report from live handles instead of a hardcoded `not_built`. |
| `GET /situation` | Track and object counts come from fusion; the over-count finding is gone because the over-count is. |
| Operator interface | Track detail shows the object roster; the Sensors page shows conditions; the shell's fusion chip is derived from the health endpoint rather than asserted. |
| `Track` | New `fusion: TrackFusion` field, written back through `apply_fusion` exactly as classification and risk are (ADR 0006, FM-025). |

### 7d. What is still not fixed

- Fusion reconciles what tracking produced; it cannot recover an object tracking never saw.
- Because the presented track is one member's estimate rather than a combination, the position
  shown is as good as the best single observation, not better than it.
- A sensor whose link is healthy and whose hardware is blind, at a site where nothing else covers
  its volume, is still undetectable. There is no evidence anywhere in the system that would
  separate it from an empty sky, and the honest response is the one taken: report the facts,
  including a detection count of zero, and decline to draw the conclusion for the operator.

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
| Fusion split-group identity | Both halves of a split group inherited the same object id; the primary flipped every cycle and alerts churned. **320 of 640 cycles duplicated an id, 708 flips.** Invisible to a green suite — found by instrumenting a running scenario. | Identity under partition |
| NaN merged unrelated tracks | `NaN > limit` is `False`, so a non-finite separation passed the extent check and merged everything it touched. Found by pentest. | Comparison against non-finite values |
| Single-link chaining | 400 tracks 10 m apart became **one** object spanning 4 km. Wanted between fragments of one drone, catastrophic across a swarm. Found by pentest. | Transitive grouping without a ceiling |
| Watchdog map grew without bound | 307 entries after churning sensor ids; nothing forgot a sensor that stopped being reported. Found by pentest. | Accumulating keyed state |
| Watchdog cried wolf | Thresholds derived from update rate flagged the healthiest sensor on the site continuously (81 s of legitimate quiet against a 5 s threshold). | Deriving a threshold from the wrong quantity |
| Scripted sensor failure did nothing | `apply_degradation` had zero callers and three scenarios named sensor ids no site defines — so the scenario written to test sensor failure tested nothing, silently. | Dead-but-correct code, again |
| Stale UI and docstring claims | The shell asserted `FUSION NOT BUILT` while the track list displayed fusion output; `/situation` and the Sensors page said the same. | Hardcoded claims about other modules |

### Patterns worth carrying forward

1. **Endpoint tests miss curve bugs.** Assert monotonicity and mid-range relationships.
2. **Docstrings here are load-bearing.** Several bugs were found by reading code *against its
   own stated reasoning*. When they disagree, one of them is a bug — decide which.
3. **Resume points must derive from what was actually stored**, never from a count of it.
4. **Anything long-lived needs periodic re-authorisation**, not just an entry check.
5. `grep` for correct-but-uncalled functions. Twice now that has found a whole feature that
   silently did nothing.
6. **A claim one module makes about another goes stale silently.** Every "not built" string that
   was hardcoded became a lie the moment the module landed, and none of them failed a test.
   Derive the claim from a handle or a health response instead of asserting it.
7. **Run it; do not only test it.** Fusion's two largest defects, the watchdog's threshold error,
   and every stale UI claim were invisible to a fully green suite. They were found by driving
   full-duration scenarios and rendering the live interface.

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
pytest -q                       # 604 passed
ruff check .                    # clean
cd frontend && npx vitest run   # 56 passed
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
  track markers, track table, honest "not assessed" on unassessed tracks, and a `FUSION ACTIVE`
  chip **derived from the health endpoint** rather than asserted in the shell. **Zero console
  errors, zero failed requests.** SPA deep-link (`/health`) returns 200 on a hard refresh.
- **Fusion against live data**: `i_swarm_saturation` reconciled 50 tracks into 26 objects;
  `GET /tracks` returned 26 and `?include_supporting=true` returned 50.
- **The watchdog against live data**: "4 of 4 sensors contributing; 1 required", and the
  scripted radar in `e_sensor_degradation` observed degrading at 100 s and going offline at
  200 s with the scenario's own operator-readable reasons.
- **Pentests against fusion and the watchdog**, which produced three real findings — the NaN
  merge, single-link chaining, and the unbounded watchdog map — all fixed and locked behind
  regression tests.
- **Deployment guard** refuses all three unsafe binds and permits loopback development.
- **Auth end to end**: admin CLI → login → gated routes 401→200, OpenAPI gated, wrong password
  refused, full audit trail written.
- **A real `SensorType.RADAR` stream** through ingestion → tracking → classification → risk to a
  sensible recommendation, with nothing downstream branching on sensor kind.
- **Not verified**: the Docker path (no daemon available, and it does not start anyway — above).

---

## 10. Where to start on new work

Roughly in order of value. The two entries that topped this list at the last handover — fusion
and the watchdog — are done (§7), so this is genuinely the next tier.

1. **Wire in the testbed radar** (§6). Now the highest-value item, and it needs the
   site-configuration decision first: `SensorPlacement` assumes a `registry_key` into the
   *simulated* registry, whose schema describes a simulator's noise model rather than a real
   unit's serial port and mount geometry.
2. **Estimate-level fusion** (§7d). Fusion currently *chooses* a presented track; combining
   member estimates would make the displayed position better than the best single observation.
   Deliberately deferred, and it needs its own ADR — it would be the first place this system
   displays a position no sensor reported.
3. **Backpressure and queue shedding** (ADR 0008). `config/environments/*.json` declares
   `queues` and nothing reads it. The reason previously given (that `system_health` was empty)
   no longer holds.
4. **Reconcile the two hysteresis configs** (§4) — pick which units the deployment surface
   speaks.
5. **Decide how a container declares its exposure** (§4, Docker) — small, and it unblocks
   `docker compose up`.
6. **Restart recovery and saturation** — see `docs/failure_modes.md`; both open.

Read `docs/failure_modes.md` before relying on any of this. FM-001 (auth), **FM-010 (sensor
silence)** and **FM-023 (one object, several tracks)** are closed; restart recovery and
saturation are not.

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
- **The tracker owns track state.** Classification, fusion and risk compute values and hand
  them back through `apply_classification` / `apply_fusion` / `apply_threat`. One owner per
  piece of state is what makes an event snapshot the whole truth (FM-025).
- **"Not assessed" and "assessed and found nothing" are different**, everywhere, all the way to
  the pixel (ADR 0011). Every new field that can be absent needs an `assessed` flag or a
  nullable block, and the interface has to render the two differently. A green light nothing
  computes is worse than no light.
- **Never hardcode a claim about another module's existence.** Derive it from a handle or a
  health response. Every such string in this repository eventually became false without failing
  a test.
- **Docstrings carry the reasoning.** If you change behaviour, change the docstring in the same
  commit; several real bugs here were divergences between the two.
