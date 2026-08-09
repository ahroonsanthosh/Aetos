# Failure modes

A living FMEA-style register. Updated as issues are found, not written once. Every entry
records what fails, what the operator sees, and what we currently do about it.

Status values: **Mitigated** (handled and tested) · **Partial** (handled, gaps remain) ·
**Accepted** (known, deliberately not addressed yet) · **Open** (not yet addressed).

---

## Known limitations accepted at V0

### FM-001 — No authentication or authorisation
**Status:** Accepted · **Severity:** High if deployed, none in current use

Aetos has no login, no roles and no authorisation checks (ADR 0008). Operator actions are
attributable in structure — every `OPERATOR_ACKNOWLEDGED` event carries an operator
identity — but that identity is asserted, not authenticated.

**Consequence:** the event log does not currently prove *who* did something. Any claim that
it constitutes an audit trail of operator action is not yet supportable.

**Mitigation:** Aetos must not be exposed to an untrusted network in its current state.

### FM-002 — Tangent-plane approximation bounds site extent
**Status:** Accepted · **Severity:** Low at intended scale

Tracking maths uses a local ENU tangent plane anchored at the site reference (ADR 0004).
Positional error grows with distance from that anchor. At C-UAS ranges the error is far
below sensor measurement uncertainty. A site spanning hundreds of kilometres would need a
different projection strategy.

**Mitigation:** validity range asserted in tests. Changing a site reference point invalidates
in-flight track state, so it is treated as immutable while a system is running.

### FM-003 — Event log grows without bound
**Status:** Open · **Severity:** Medium over long deployments

No retention or archival policy exists (ADR 0006). Disk consumption grows monotonically.

**Constraint on any future fix:** the log's value is that it is immutable and complete. Any
retention scheme must preserve that property for whatever window it claims to cover, rather
than quietly truncating history.

### FM-004 — Altitude datum mismatch between systems
**Status:** Partial · **Severity:** High if it occurs

Altitude is metres above the **WGS-84 ellipsoid** throughout Aetos. Real sensors variously
report height above mean sea level, height above ground level, or barometric altitude.
Confusing these is a classic silent error of tens to hundreds of metres.

**Mitigation:** the datum is stated explicitly in `docs/detection_schema.md`. Any future real
sensor adapter is responsible for converting to the canonical datum, and that conversion is
the adapter's documented obligation.

---

## Sensor and input failures

### FM-010 — Sensor goes silent
**Status:** Open — target of build stage 12

A sensor stops producing detections. Tracks that depended on it coast, then go STALE, then
LOST. The hazard is that the system keeps rendering confident-looking tracks on data that no
longer exists.

**Intended handling:** watchdog detects sensor silence past a configured threshold; sensor
health is marked degraded; if sensor count or aggregate confidence drops below the usable
threshold, degraded mode activates and is stated visibly in the UI.

### FM-011 — Malformed detection data
**Status:** Open — target of build stage 12

A sensor emits data violating the canonical schema.

**Intended handling:** rejected at the ingestion boundary by schema validation, logged as an
event with the reason, never propagated to tracking. One bad sensor must not stop the others.

### FM-012 — Clock skew between sensors
**Status:** Open — target of build stage 12

Two sensors disagree about the current time. Detections of the same object arrive with
timestamps that imply impossible motion, corrupting the filter's velocity estimate.

**Intended handling:** per-sensor skew estimation, bounds on acceptable skew, and rejection
with an event beyond those bounds.

### FM-013 — Duplicate detections
**Status:** Open — target of build stage 12

The same detection arrives more than once. Naively processed, it double-weights the update
and makes the filter overconfident.

**Intended handling:** idempotency on `detection_id` at the ingestion boundary.

### FM-014 — Saturation / swarm overload
**Status:** Open — target of build stage 12

More simultaneous objects than the system can process in real time.

**Intended handling:** bounded queues with an explicit shed policy (ADR 0008). Established
confirmed tracks keep updating; new-track acquisition degrades first. Every shed input is
recorded as an event. Sustained shedding activates degraded mode.

---

## Tracking failures

### FM-020 — Identity swap at track crossing
**Status:** Open — target of build stage 3

Two objects cross; the tracker exchanges their identities. Greedy nearest-neighbour
association fails here by construction.

**Intended handling:** Mahalanobis-gated Global Nearest Neighbour association (ADR 0005),
with velocity state disambiguating objects whose positions coincide. Scenario H is the
acceptance test.

### FM-021 — Tracker silently depends on simulator ground truth
**Status:** Open — target of build stage 3

The most dangerous failure in the project, because it is invisible until real hardware
arrives. If the tracker consumes `kinematics` or `classification` from detections, it is
consuming information the simulator has and real sensors do not.

**Intended handling:** the guard test in ADR 0002 — a full scenario run with those fields
stripped must produce identical track states.

### FM-023 — One object becomes several tracks under multi-sensor observation
**Status:** Open — target of build stage 10 (fusion) · **Severity:** High for operator trust

**Observed**, not theorised: running `b_approaching_threat` against the shipped test site,
one drone produced three concurrent CONFIRMED tracks (TRK-00002, TRK-00009, TRK-00018), all
at the same range and bearing with consistent kinematics.

**Cause.** Three sensors observe the object, so each cycle delivers three detections of it.
Global nearest neighbour assigns at most one detection per track (ADR 0005), which is correct
— two detections of *different* objects must never collapse into one track. The surplus
detections are therefore unassigned, and unassigned detections initiate new tracks. Every step
behaves exactly as designed; what is missing is the stage that reconciles observations of one
object across sensors.

**Consequence.** An operator sees three contacts where there is one. Under a swarm scenario
the multiplier is the sensor count, which also brings the saturation ceiling forward.

**Intended handling:** `backend/fusion`, which is not built. Note that the fix belongs there
and not in association — loosening the association gate to absorb the extra detections would
merge genuinely distinct objects, which is a far worse failure and is precisely what scenario
H tests against.

**Interim mitigation:** none. A single-sensor site does not exhibit it. The operator interface
does not hide or de-duplicate the tracks, because suppressing them in the display would
conceal a real limitation of the current build.

### FM-026 — Risk could not see relationships between tracks
**Status:** Closed, with an interim mechanism · **Severity:** was Medium

Every risk factor assesses one track alone, by design — the narrow input is what stops a factor
depending on the previous risk level, which is how a score becomes self-reinforcing. But
several contacts converging on the asset from different bearings is a situation no per-track
factor can describe, and the engine scored each of them exactly as it would score one arriving
alone.

**Fix.** `backend/risk/correlation.py` runs a cycle-level pass *after* per-track scoring and
produces a **separate finding**, served from `GET /situation`. It never modifies any track's
score — `tests/unit/test_risk_correlation.py` drives the real service with one track and then
with a full coordinated approach and asserts the subject's score is identical, because the
alternative is a feedback loop with no evidence in it: a busy sky raises every score, which
raises the count of alarming tracks, which raises every score.

Findings: `coordinated_approach` (contacts closing from bearings far enough apart to be
separate axes), `simultaneous_engagement` (more objects engaged at once than an operator can
reasonably attend to), and `duplicate_tracks_suspected` — see below.

**The FM-023 problem, and why this did not have to wait for fusion.** Counting *tracks* would
have reported one drone seen by four sensors as a four-axis coordinated approach. Fragments of
one object and genuinely distinct contacts are separable without doing fusion's job, though:
fragments are co-located **and** co-moving, because they are observations of the same thing,
while contacts converging on an asset are hundreds of metres apart on different bearings. So
tracks are clustered on that test and the situation is assessed over clusters. Verified live:
on `d_multiple_simultaneous` the pass resolved 8 tracks to 5 objects and counted objects, not
tracks, when deciding whether the picture was saturated.

**This is not fusion and does not claim to be.** It merges nothing, changes no track state,
re-runs no association, and its output is scoped to risk. It answers one question — how many
distinct objects are these tracks likely to be — because the situation assessment cannot ask
anything sensible without it. When `backend/fusion` exists it answers that question properly
and the clustering here is deleted.

**Remaining, and deliberately visible.** The over-count is now *reported* rather than silently
absorbed: `duplicate_tracks_suspected` states plainly that the display is showing more tracks
than there are objects, and the banner carries "8 tracks · likely 5 objects". That is FM-023
made visible to the operator instead of left for them to discover.

### FM-025 — Classification was overwritten by the next tracking update
**Status:** Closed · **Severity:** High while open — the interface stated the opposite of what the system had concluded

**Observed** by querying `GET /tracks` on a running system after classification was built.
Every track reported `unknown` at zero confidence, while the event log held 66
`CLASSIFICATION_CHANGED` events with correct labels.

**Cause.** `ClassificationService` applied its result to a *copy* of the track and recorded
that copy in the log. The tracker — which owns track state and emits a full snapshot on every
track event — knew nothing about it. Each cycle therefore ran: tracking writes
`TRACK_UPDATED` with an unclassified snapshot, classification writes
`CLASSIFICATION_CHANGED` with a classified one. Within a cycle classification won on
sequence; on the *next* cycle tracking overwrote it again, and because
`CLASSIFICATION_CHANGED` is only emitted on a significant change, most cycles left the last
word with tracking.

**Why the tests missed it.** Every unit test asserted on the value `classify_cycle` returned
— which was correct throughout — and the integration checks counted events, which were
present and correct too. Nothing asserted on what a consumer of the API would actually read.

**Fix.** `TrackManager.apply_classification` — classification hands its answer to the owner of
the state instead of holding a second copy, and the tracker's snapshots carry it from then on.
One owner per piece of state is also what keeps the log replayable into the state the live
system holds (ADR 0006). `tests/unit/test_classification_service.py` pins it by asserting
through `active_tracks()` and through a `TRACK_UPDATED` snapshot, never through the returned
copy.

**Related:** the same class of defect — a stale field silently winning — is possible for any
future module that produces a value belonging to a track. Risk scoring must use the same
write-back path, not a copy.

### FM-024 — Classification sees only one sensor's hint per track
**Status:** Open — resolved by fusion (build stage 10) · **Severity:** Medium

**Observed** while verifying classification against `d_multiple_simultaneous`: of 189
classification assessments, only 34 had any sensor-reported evidence at all. The rest were
kinematic-only.

**Cause.** Association assigns at most one detection to a track per cycle (ADR 0005, and
correctly so). Classification therefore receives the hint from whichever single detection won
that assignment. At the shipped test site, two of four active sensors are radar profiles that
report no classification at all, so a track whose assigned detection came from a radar gets no
sensor evidence that cycle — even though an EO/IR or RF sensor observed the same object and
did report one. Compounded by FM-023: because one object becomes several tracks, each track
tends to be dominated by a single sensor, so a radar-formed track may *never* see the EO/IR
hint for the same physical object.

**Consequence.** Classification is weaker than the available evidence justifies, and leans on
kinematic envelopes, which overlap heavily between classes and discriminate poorly on their
own. Confidence is correspondingly low, which is at least honest.

**Intended handling:** `backend/fusion`. Once observations of one object are reconciled across
sensors, every hint for that object reaches the classifier together, which is also when
`SensorHintEvidence`'s agreement/disagreement handling starts doing real work — it is
implemented and tested, but currently sees one report at a time in most cycles.

**Not a workaround candidate:** routing *unassigned* detections' hints to nearby tracks would
be association by another name, done worse and outside the module that owns it.

### FM-022 — Track fragmentation on reacquisition
**Status:** Open — target of build stage 3

An object lost and then redetected becomes a new track with a new ID, so history is
fragmented and the operator sees a "new" object that is not new.

**Intended handling:** coasting on predicted position through brief dropout before demoting
state; track history retained after LOST rather than deleted.

---

## System failures

### FM-030 — Backend restart
**Status:** Open — target of build stage 12

**Intended handling:** in-memory state is a projection of the event log (ADR 0006), so state
is rebuilt by replaying from the log. Recovery is a chaos test, not an assumption.

### FM-031 — UI crash silencing alerts
**Status:** Open — target of build stage 12

If alerting lives in the dashboard process, losing the dashboard loses alerting silently —
the worst possible failure, because nothing indicates anything is wrong.

**Intended handling:** alert generation is decoupled from the UI process (spec §16). Alerts
are produced and persisted backend-side; the UI is a viewer.

### FM-032 — Database unavailable
**Status:** Open — target of build stage 12

The event store cannot be written. Since nothing downstream may read an event before it is
durably appended, this halts the pipeline by design.

**Intended handling:** fail loudly and visibly rather than continuing to track without an
audit trail. Continuing without durable events would produce confident output that cannot be
reconstructed or defended afterwards.

---

## Deployment

### FM-041 — The basemap installer's download path is unverified
**Status:** Open · **Severity:** Low — affects an optional feature's setup, not runtime behaviour

`install-basemap.sh` was written and tested in an environment whose network policy blocks
`api.github.com`, `build.protomaps.com` and `github.com`. Everything that does not require the
network is verified: site lookup, bounding-box derivation (checked at four latitudes, each
producing exactly the requested span), platform detection, argument handling, archive
validation by magic bytes, and every failure message. The three download steps have **never
been run successfully**, only observed to fail cleanly with actionable messages.

Two assumptions in particular are unconfirmed:

- The GitHub release asset naming (`..._${OS}_${ARCH}.tar.gz`) is taken from the go-pmtiles
  release convention and is matched by pattern rather than pinned, so a naming change breaks it.
  It fails with a message pointing at the releases page, which is the right failure, but it is
  a failure.
- Protomaps publishes dated planet builds with no "latest" alias, so the script walks back up
  to fourteen days looking for one. If the publishing cadence is slower than that it finds
  nothing and reports it.

**Mitigation:** `docs/basemap.md` documents the manual path in full, and every automated step
fails with the manual equivalent. The basemap is optional throughout — with nothing installed
the display is exactly as it would otherwise be.

---

### FM-040 — Docker image is unverified
**Status:** Open · **Severity:** Low — affects setup only, not runtime behaviour

`Dockerfile` and `docker-compose.yml` were written but **never built or run**: the environment
they were authored in had no Docker daemon. `start.sh` and the single-process serving it
relies on are verified end to end in a browser; the container path is not.

One issue was caught by inspection rather than by testing and is already fixed: a regular
`pip install .` places `backend/` in site-packages, and `ConfigPaths.default()` derives the
repository root from its own `__file__` — so configuration loading would have looked beside
site-packages and failed. The image installs editable for that reason. Other issues of the
same kind may remain.

**Mitigation:** `./start.sh` is the verified path. Anyone using the container should expect to
debug the first build.
