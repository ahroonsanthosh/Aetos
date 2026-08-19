# Failure modes

A living FMEA-style register. Updated as issues are found, not written once. Every entry
records what fails, what the operator sees, and what we currently do about it.

Status values: **Mitigated** (handled and tested) · **Partial** (handled, gaps remain) ·
**Accepted** (known, deliberately not addressed yet) · **Open** (not yet addressed).

---

## Known limitations accepted at V0

### FM-001 — No authentication or authorisation
**Status:** Closed · **Severity:** was High if deployed

**What it was.** Aetos had no login, no roles and no authorisation checks (ADR 0008 §3, now
superseded). Operator actions were attributable in structure — every `OPERATOR_ACKNOWLEDGED`
event carried an operator identity — but that identity was asserted, not authenticated, so the
log could not prove *who* did anything. Anyone who could reach the port had the full operator
interface and the complete event log.

**Fix.** `backend/auth/` and ADR 0016. Session cookies with a two-sided expiry, three ordered
roles, scrypt password hashing, lockout, and an audit trail of login, logout and lockout.
`Identity.authenticated` is now true and `OperatorContext.basis` reads `session` — both fields
existed from the first commit for exactly this day, which is what makes the two eras of the log
distinguishable rather than silently merged.

**What made it stay closed.** `tests/integration/test_auth_api.py` walks the application's own
route table and makes an unauthenticated request to every route, failing unless each is either
gated or on a short named public list. A new endpoint added without a gate fails the suite
without anyone having to remember the test exists. Three deliberate mutations — removing the
router gate, accepting any authenticated user, accepting unauthenticated WebSocket upgrades —
were each caught.

**And the deployment posture is enforced rather than documented.** `guard_bind` refuses to
start with authentication disabled off loopback, with no accounts off loopback, or off loopback
without `--behind-proxy`. Aetos does not terminate TLS; that flag is an explicit assertion that
something in front of it does. See `docs/deployment.md`.

**What is still open, and deliberately named.** No password reset flow, no multi-factor, no
administrator view of active sessions. `LocalOperatorIdentityProvider` still exists for
single-user machines and tests, so a deployment *can* run unauthenticated — bounded by the
startup guard rather than by convention, and `GET /system/health` says so in as many words when
it happens.

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
**Status:** Closed — `backend/system_health` (ADR 0018)

A sensor stops producing detections. Tracks that depended on it coast, then go STALE, then
LOST. The hazard is that the system keeps rendering confident-looking tracks on data that no
longer exists.

**Fix.** `SensorWatchdog` polls every adapter's health record each cycle and reports a condition
per sensor, with degraded mode when contributing sensors fall below the configured floor.
`GET /system/health` shows it, and `SENSOR_STATUS_CHANGED` / `DEGRADED_MODE_ENTERED` record every
transition.

The difficulty was never the polling. **A sensor producing nothing may be broken, or may be
looking at an empty sky**, and one sensor watched alone cannot tell those apart — guess "broken"
and the indicator fires through every quiet minute until it is ignored, guess "empty sky" and it
stays quiet through the event it exists to catch. Silence is therefore judged against *the other
sensors*: anomalous while neighbours are producing, an empty sky when nothing anywhere is. A
single-sensor site can never call its sensor silent and says so, rather than implying a check it
cannot perform.

Thresholds are declared per sensor rather than derived from update rate. Deriving them flagged
the healthiest sensor on the site continuously: poll rate is not detection rate, and the
narrow-field EO/IR's longest *legitimate* quiet spell across all nine scenarios was 81 s against
a derived threshold of 5 s.

**What is still not detectable:** a sensor whose link is healthy and whose hardware is blind, at
a site where nothing else covers its volume. No evidence in the system separates that from an
empty sky, so the facts are reported — including a detection count of zero — and the conclusion
is left to the operator.

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
**Status:** Closed — `backend/fusion` (ADR 0017) · **Severity:** was High for operator trust

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

**Fix.** `backend/fusion` runs between tracking and classification and groups tracks that are
co-located *and* co-moving into objects. Everything downstream — classification, risk, alerting,
the API — counts objects rather than observations, so one drone is classified once, scored once
and alerted on once. `GET /tracks` returns one entry per object; `?include_supporting=true`
returns the members, which are withheld rather than deleted.

The fix is downstream of association on purpose, exactly as this entry originally argued:
loosening the gate would merge genuinely distinct objects, which is worse and is what scenario H
tests against. Fusion changes no estimate, so being wrong costs a display grouping rather than a
corrupted track.

Measured against the declared truth of all nine scenarios, object counts now match. Alert volume
fell *below* the pre-fusion baseline — 3 → 1 on `b_approaching_threat`, 54 → 28 on
`i_swarm_saturation` — because one object no longer raises one alert per sensor that can see it.

Two defects found during the build are worth recording, because both were invisible to reasoning
and obvious to measurement: identity had to be resolved across the whole cycle rather than per
group (320 of 640 cycles otherwise held a duplicated object id, flipping the presented track 708
times and thrashing alerts), and single-link grouping needed a hard extent ceiling (400 tracks
spaced 10 m apart otherwise chained into one object spanning 4 km).

**What is still not fixed:** the presented position is one member's estimate rather than a
combination of them, so it is as good as the best single observation and no better. Estimate-level
fusion is a deliberate future step behind its own decision record.

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
separate axes) and `simultaneous_engagement` (more objects engaged at once than an operator can
reasonably attend to).

**It used to carry its own clustering, and no longer does.** While `backend/fusion` did not
exist, counting *tracks* would have reported one drone seen by four sensors as a four-axis
coordinated approach, so this module grouped co-located and co-moving tracks itself — described
throughout as a stand-in to be deleted once fusion answered the question properly. Fusion landed
(ADR 0017) and the clustering was deleted.

Leaving it would have been worse than redundant. Fusion holds a group together across a
hysteresis band, so two objects it deliberately keeps apart can sit closer than a
single-threshold test would merge them — and two subsystems answering "are these the same
object" differently, in front of the same operator, is worse than either answer alone. Its input
is now one entry per object, and the track-versus-object counts on the banner come from fusion,
which owns that relationship.

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
did report one. This was compounded by FM-023 while that was open: one object became several
tracks, each tending to be dominated by a single sensor, so a radar-formed track might *never*
see the EO/IR hint for the same physical object. Fusion (ADR 0017) reconciles those tracks into
one object, but does **not** pool their classification evidence — the hint still reaches only
the track whose detection carried it, and only the presented member's classification is shown.
Pooling evidence across an object's members is the obvious next step and is not built.

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
**Status:** Partial — alerting is backend-side; the chaos test is not written

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

- The GitHub release asset naming (`..._${OS}_${ARCH}.<ext>`) is taken from the go-pmtiles
  release convention and matched by pattern rather than pinned, so a naming change breaks it.
  It fails with a message pointing at the releases page, which is the right failure, but it is
  a failure. The Windows variant (`.zip` containing `pmtiles.exe`) and the Unix one (`.tar.gz`
  containing `pmtiles`) are both handled; only the Unix path could be exercised even as far as
  the request.
- Protomaps publishes dated planet builds with no "latest" alias, so the script walks back up
  to fourteen days looking for one. If the publishing cadence is slower than that it finds
  nothing and reports it.

Unpacking is *not* among the unverified parts. It goes through
`scripts/extract_archive.py` rather than `unzip` or `tar`, because Git Bash — the shell every
Windows user of this repository is in — ships neither reliably, and the extractor is tested
locally across zip and tar archives, flat and structured extraction, a no-match failure, and a
path-escape attempt.

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
