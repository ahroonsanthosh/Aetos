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
**Status:** Closed — `backend/system_health`

A sensor stops producing detections. Tracks that depended on it coast, then go STALE, then
LOST. The hazard is that the system keeps rendering confident-looking tracks on data that no
longer exists — and, worse, that an empty map is read as a clear sky.

**Handling.** The watchdog runs as a cycle stage, holding the window and the site policy that
no adapter can hold for itself, and it draws a distinction the rest of the system is not
permitted to blur:

- **Loss of contact is definite.** A source reporting OFFLINE, or one whose poll raised, is
  faulted. The posture becomes DEGRADED with any source out and BLIND with all of them out,
  and both are recorded as `DEGRADED_MODE_ENTERED` and shown as a banner above the component
  list.
- **Absence of detections is not.** A source in contact and silent past this site's threshold
  is reported QUIET, carrying the seconds and the threshold that produced the verdict — never
  as a fault, because an empty sky produces identical evidence. The system states the fact and
  leaves the conclusion to the operator.

Thresholds are per sensor type in `config/system_health/policy.json`, because the right window
differs by an order of magnitude between an imaging sensor and a radio-frequency one.

**The stall.** Every component here reports on itself, and a component that has stopped running
reports nothing at all — which renders identically to one with nothing to say. The watchdog
timestamps each pass and `report()` ages it against the clock at the moment somebody asks, so a
pipeline that has stopped stepping shows as STALLED even though nothing inside it is in any
position to notice. This is the self-test, and it works only because it is evaluated outside
the thing it tests.

**What degraded mode deliberately does not do:** change behaviour. No threshold moves and no
track is treated differently. Silently altering the assessment pipeline when the picture thins
would mean an operator comparing two incidents was comparing two different systems. The
declaration is the product.

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

### FM-012a — Sensor clock skew silently shortened the coast budget
**Status:** Closed · **Severity:** was Medium — fragmented tracks, only at sites with
unsynchronised sensors

**What it was.** Track staleness was measured as `now - last_seen`: a pipeline instant minus a
*sensor* instant. The difference therefore carried whatever offset existed between the two
clocks. Ingestion accepts up to 30 s of skew (FM-012 proper), so a sensor lagging 25 s produced
confirmed tracks that were **retired on their first missed cycle** instead of coasting for the
configured six seconds. The object reappeared as a new track with a new identity and a split
history — exactly the fragmentation `coast_seconds` exists to prevent (FM-022).

Nothing reported it. The lifecycle thresholds simply meant a different duration than the one
they named, and by how much depended on how well someone's sensors happened to be synchronised.

**Fix.** `_ActiveTrack.last_supported` records when a track last received a detection on the
**pipeline's** clock, and demotion is driven from that. `last_seen` remains the sensor's
observation time and is still published as-is — the two are different questions and now have
different fields. `last_updated` could not serve: prediction advances it every cycle, including
for coasting tracks.

**What makes it stay closed.** `tests/unit/test_track_manager.py` runs the coast test
parametrised across 0, 3, 10 and 25 seconds of sensor lag and requires identical behaviour,
alongside a test that real elapsed time still demotes an unsupported track — so the fix cannot
degrade into "tracks never go stale" — and one asserting observation time is still reported
from the sensor rather than restamped on receipt.

### FM-027 — Risk policy parameters were the one unvalidated configuration surface
**Status:** Closed · **Severity:** was Medium — a threat factor silently disabled, or a killed
pipeline cycle

**What it was.** `FactorPolicy.parameters` is a free-form `dict[str, float]`, read by name with
a documented default. Nothing validated the values, and both ways they could be wrong were
silent:

- **Negative.** `fast_mps: -25` does not disable the closing-velocity factor, it inverts it: a
  target closing at 20 m/s scored an activation of zero. A threat factor switched off by a
  typo, with nothing anywhere reporting it.
- **Zero, where the value is a divisor.** `ZeroDivisionError` raised from inside the
  assessment, killing the risk stage and with it the whole pipeline cycle — the operator's
  picture stops advancing, and the reason appears only in a log.

The second had a second cause worth naming: three of the five division sites in
`backend/risk/factors.py` already carried a `max(1e-6, ...)` guard and two did not. The idiom
existed and had been applied inconsistently, which is how a defended pattern still produces an
undefended path.

**Fix.** Parameters are validated at load — finite, and not negative — with a message naming
the parameter, consistent with every other configuration surface in this repository. Zero
remains legal, because several parameters are floors where zero honestly means "no minimum",
and the two unguarded divisors were brought into line with the three that were already safe.

**What makes it stay closed.** `tests/unit/test_risk.py` covers the negative and non-finite
rejections, asserts zero is still accepted so the validator cannot overreach into breaking
legitimate configurations, and drives both previously unguarded divisors with a zero parameter
to assert the cycle survives.

### FM-028 — Track history was unbounded, and truncated from the wrong end
**Status:** Closed · **Severity:** was Medium — a partial route presented as a complete one

**What it was.** `GET /tracks/{id}/history` declared a ceiling of 10 000 points on its `limit`
parameter, and defaulted that parameter to `None` — meaning no limit at all. Track history is
retained permanently (FM-022), so the default response was the entire history of a track, for
as long as a deployment had been running. The operator interface requested it with no limit.

The second half is worse. When a limit *was* supplied, the query was `ORDER BY seq LIMIT n`,
which returns the **oldest** n entries. An operator asking where a track had been received the
beginning of its route, with nothing in the response indicating that the rest existed — a trail
drawn from it looks exactly like a complete one.

**Fix.** The endpoint is bounded by default rather than on request. Truncation keeps the most
recent points, because where something has been lately is the operationally useful half, and
the response carries `total` and `truncated` so a partial history is visibly partial. The map
legend says so in words when a trail is truncated.

**What makes it stay closed.** `tests/integration/test_api.py` asserts the default response is
bounded, that a truncated one reports `truncated` with the true `total`, that the points
returned are the most recent ones rather than the first, and that they remain oldest-first so
the trail is still drawn in the right direction.

### FM-029 — "Last observed" was measured across two unsynchronised clocks
**Status:** Closed · **Severity:** was Medium — a healthy sensor displayed as long-dead

**What it was.** The track detail panel computed staleness as `Date.now() - last_seen`: the
**browser's** clock minus the **sensor's** observation timestamp. Neither machine is one this
system controls, and the drift between them landed directly in the figure an operator reads as
"Last observed N s ago".

An operator laptop a minute out of sync — routine — displayed every track as a minute stale,
including ones being updated twice a second. A sensor clock running fast produced a negative
age. The same mistake as FM-012a, one layer further out and with a third clock involved.

**Fix.** The backend reports the figure. `TrackMetadata.seconds_since_detection` is computed by
the tracker on the pipeline clock — the same measurement that drives demotion — and served
through the API, so every consumer displays one number computed once by the component that
knows. The field is nullable and rendered as "—" when absent, because a missing figure must
never read as "seen just now", and snapshots written before the field exists genuinely do not
carry it.

**What makes it stay closed.** `frontend/tests/unit/track.test.ts` asserts the reported value
is taken as given and that a null stays null rather than collapsing to zero.

### FM-0210 — Risk read an ellipsoidal altitude against site-relative thresholds
**Status:** Closed · **Severity:** was High at any elevated site — a threat factor silently
switched off

**What it was.** Two vertical quantities travelled into the risk engine and the factors read
the wrong one. `TrackRisk.position.up_m` is height above the site reference plane;
`TrackRisk.altitude_m` was the track's **ellipsoidal** altitude, taken straight from the
published `Track`. They differ by the site's own elevation.

`low_altitude_near_asset` compares against `low_m: 120` and `very_low_m: 30` — thresholds that
plainly mean height above the site. At a site 500 m above the ellipsoid, a drone forty metres
over the fence was assessed as being at 540 m, and the factor reported "not a low-altitude
approach". It could never fire at all above 120 m of site elevation.

Nothing failed. The shipped fictional site sits at 5 m, so every test and every demonstration
looked correct, and the defect was a property of *where the system was installed*.

**Fix.** Risk carries one vertical quantity, on one datum: `TrackRisk.altitude_m` is height
above the site reference and is populated from `track.enu.up_m`. `backend/zones` already worked
this way — it converts the site document's ellipsoidal floors and ceilings into ENU at the
boundary rather than carrying both — and risk now matches it. The published `Track` still
reports ellipsoidal altitude, which is correct for display and for FM-004.

**What makes it stay closed.** `tests/unit/test_risk.py` scores the same low approach at sites
0, 5, 200, 500 and 1 500 m above the ellipsoid and requires the same answer, plus a test on the
mapping itself — because the factor is correct in isolation either way, and it was the value
flowing into it that carried the wrong datum.

### FM-0211 — Classification read an ellipsoidal altitude against site-relative bands
**Status:** Closed · **Severity:** was High at any elevated site — systematic misclassification

**What it was.** The same mistake as FM-0210, in the other assessment module and found by
looking for it. `ClassificationService` passed `track.position.altitude_m` — ellipsoidal — into
`TrackEvidence.altitude_m`, which is matched against the bands in
`config/classification/profiles.json`.

Those bands are unambiguously site-relative, and the `vehicle` profile says so in the data:
`absolute_min: -30.0`, `typical_max: 15.0`, `absolute_max: 40.0`. A band straddling zero is only
meaningful as height above the ground the vehicle is standing on.

So the site's elevation was added to every track before classification. The same drone hovering
thirty metres up classified as `small_uas` at a sea-level site, leaned `bird` at two hundred
metres, and became `unknown_uas` with full confidence at a kilometre.

**Why it is worse than a wrong label.** A benign classification *discounts* the risk score
(`mitigation_for`). At a few hundred metres of site elevation the error pushed real contacts
toward `bird`, which then made the system more willing to dismiss them. The two defects
compounded in the dangerous direction.

**Fix.** `TrackEvidence.altitude_m` is height above the site reference, from `track.enu.up_m`,
documented on the field.

**What makes it stay closed.** `tests/unit/test_vertical_datum.py` covers the whole class rather
than the two instances: it asserts that the same approach scores identically and produces the
same reason codes at sites from sea level to 1 500 m, that the same object classifies
identically at every elevation, and — pinned by name, because a property test needs a site to
fail at — that neither assessment module reads the ellipsoidal field.

### FM-060 — Password hashing ran on the event loop
**Status:** Closed · **Severity:** was High — unauthenticated denial of service against the
operator's picture

**What it was.** `POST /auth/login` is an `async` handler and called `AuthService.authenticate`
directly. Password verification is scrypt at deliberate cost — around 350 ms — so it executed
**on the event loop**, and everything the process does shares that loop: other requests, the
WebSocket push, and the pipeline cycle that advances the operator's display.

Measured, not theorised. Three unauthenticated clients sending login attempts took every other
request from 0.7 ms to 1.1 seconds — a factor of 1683. No credentials, no exploit, just the
login form. And it is worse than slow responses: with the loop blocked, the pipeline stops
stepping and the stream stops pushing, so the display freezes on stale data while showing every
sign of being live.

**Fix.** Authentication is split into three phases — read, verify, record. Only the verify step
leaves the loop, in a worker thread, so every database read and write stays on the single writer
that owns them (ADR 0006). The synchronous `authenticate` remains for the admin CLI and is a
thin wrapper over the same three phases, so there is one implementation of the decision logic
rather than two that can drift.

Concurrency is bounded (`MAX_CONCURRENT_HASHES`). Moving the hash off the loop stops a flood
freezing the display; it does not stop one burning every core, and an unauthenticated caller
chooses how many to send. A flood now queues instead of saturating.

**What makes it stay closed.** `tests/integration/test_network_exposure.py` reproduces the
attack and asserts responsiveness holds — verified to fail when the fix is reverted. The
timing-equalisation that resists account enumeration is re-asserted alongside it, because the
refactor moved `dummy_verify` and a version that returned early for an unknown user would have
reintroduced the oracle while every other test passed.

### FM-061 — No security headers
**Status:** Closed · **Severity:** was Medium — clickjacking and MIME sniffing against a browser
console

**What it was.** No `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options`,
`Referrer-Policy` or `Permissions-Policy` on any response. The realistic attack on an operator
console is clickjacking: frame it invisibly and let someone acknowledge an alert they cannot
see.

**Fix.** Middleware, so a route added later cannot be added without them — the same reasoning
that gates the router rather than each handler. Set in the application rather than left to the
reverse proxy: a proxy is the right place for TLS and rate limiting, which this process cannot
do, but a header it *can* set should not depend on someone else's configuration being right.
`Strict-Transport-Security` is the deliberate exception, added only to requests that arrived
over TLS so a loopback run cannot pin `localhost` to https in a developer's browser.

### FM-062 — Scenario control was gated at viewer
**Status:** Closed · **Severity:** was Low today, High on the day the engine is written

**What it was.** `POST /scenarios/{id}/start` and `/stop` carried only the router's baseline
`viewer` gate, while `POST /scenarios` required `admin`. Both are unimplemented, so nothing was
exposed — but the missing role was deferred to whoever implemented the engine, in a docstring
saying it "becomes an operator action" one day. The route existed with viewer access, and the
gate depended on a future author reading a comment.

**Fix.** Gated at `admin` now, while it does nothing. A viewer is not authorised to drive the
simulation whether or not the engine that would do it exists.

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
**Status:** Closed — `backend/fusion` (ADR 0020), with a stated limit · **Severity:** was High for operator trust

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

**Fix.** `backend/fusion` reconciles a cycle's detections into one measurement per object
*before* tracking, by inverse-covariance weighting, on the tracker's own statistical gate (ADR
0020). It does not live in association: loosening the association gate to absorb the extra
detections would merge genuinely distinct objects, a far worse failure and precisely what
scenario H tests against. Measured: `b_approaching_threat` goes from three CONFIRMED tracks to
one; `h_track_crossing` from six to four. Real and simulated detections are never fused, and a
fused track lists the sensors that actually saw it.

**Residual, by design.** A detection that could belong to either of two objects, or is too
uncertain to place, is left unfused — an over-count the operator can see rather than a merge
they cannot. Two objects closer than the sensor noise may transiently merge (~1.6% of
multi-sensor groups on `h_track_crossing`, bounded by a test); that is an information limit,
not tuning. The situation assessment's `duplicate_tracks_suspected` finding (FM-026) stays as
the operator's signal when duplicates do occur.

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
**Status:** Closed — `backend/fusion` (ADR 0020) · **Severity:** was Medium

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

**Fix.** With observations reconciled across sensors, a fused measurement carries the ids of
every detection it was built from, and `CycleEvidence.from_fused_assignments` routes *every*
contributing sensor's hint to the track — so `SensorHintEvidence`'s agreement and disagreement
handling now sees several reports at once. Without that mapping, fusion would have made things
worse: the fused id is not a key in ingestion's hint map, so a multi-sensor object would have
received no hint at all. A test pins it.

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

### FM-040 — The container files exist, are broken, and the README used to claim otherwise
**Status:** Open · **Severity:** Low — affects setup only, not runtime behaviour

This entry previously said no `Dockerfile` or `docker-compose.yml` had ever existed in this
repository, and removed a README section telling people to run `docker compose up` from the
repository root. That correction was itself wrong: it checked only the repository root.
`frontend/Dockerfile` and `frontend/docker-compose.yml` do exist, at that location, and are
broken in a way that made the removed README instruction fail regardless —

1. **Wrong location.** The Dockerfile `COPY`s `pyproject.toml`, `backend/`, `sensors/`,
   `scenarios/` and `config/`, none of which exist under `frontend/`. Compose sets `build: .`
   from that directory, so the build context cannot resolve the Dockerfile's own paths.
2. **Stale security claims.** Both files assert Aetos "has no authentication of any kind
   (FM-001)". FM-001 has been closed since ADR 0016; the port should stay loopback-only
   because an operator console carrying the whole live picture has no business being reached
   from off the machine, not because there is no login.
3. **The container would not start even relocated.** It must bind `0.0.0.0` to be reachable
   from outside itself, and `guard_bind` refuses that without `--behind-proxy`, which asserts
   TLS is terminated in front of the process — an assertion a bind-to-loopback compose file
   cannot make truthfully. This needs a decision, not a flag.

A sibling exploration branch (`claude/examine-codebase-sck2hv`, commit `9335457`) relocated
the files to the repository root and corrected the authentication claim, confirming the third
point by running `guard_bind('0.0.0.0', authenticated=True, behind_proxy=False, user_count=0)`
and observing `StartupRefused`. That work was never merged and predates live mode, the
watchdog, and the network-hardening pass on this branch — it should be re-verified against
the current CLI and startup sequence before being pulled in, not merged blind.

**Until resolved:** `./start.sh` is the only container-free, verified, supported way to run
Aetos, and the README says so without mentioning Docker at all — removing the instruction was
correct; the diagnosis of why it had to be removed was not.

---

## Testbed radar hardware

The three entries below concern the serial-attached FMCW radar in
`sensors/adapters/uart_fmcw_radar/` (ADR 0017). All three are properties of the *hardware*,
not defects in the driver, and none of them is fixable in software. They are recorded because
the alternative — a driver that quietly papers over them — produces an operator display that
looks the same whether it is right or wrong.

The adapter is constructed only in LIVE or HYBRID mode, against a site that declares hardware
(ADR 0018). A default `./start.sh` run is unaffected by all three. A `./start.sh --live` run is
affected by all three, which is why that mode warns about them at start-up rather than relying
on this document being read.

### FM-053 — A live run inherited a simulation run's tracks
**Status:** Closed · **Severity:** was Critical — synthetic tracks on a live display

**What it was.** The event log and its projection lived in one `data/` directory, shared by
every run. The projection survives restarts by design (ADR 0006, FM-030), so starting
`--mode live` after a scenario **inherited that scenario's tracks and served them under a LIVE
badge**: `simulated: false`, four synthetic tracks, and nothing in the data that could have
told the operator.

Every component was reporting truthfully. Real adapters *were* attached, so the badge was
right. The tracks were real records of things that had really been tracked — in a different
run, of a different kind. The falsehood was in the assembly, which is what made it invisible
from inside any one module.

**How it was found.** By running the system the way a user does — a scenario, then `--live`,
then reading `GET /tracks` — rather than by testing the parts. Nothing in the unit or
integration suites could have caught it: every test built its own pipeline in its own
temporary directory, which is precisely the isolation the product did not have.

**Fix.** Persistent state is partitioned per deployment: `data/<site>/<mode>/`. A different
site, or a different run mode, is a different history. A `deployment.json` marker in the
directory records which deployment owns it, and start-up refuses rather than proceeding when
it disagrees — which also covers an explicitly chosen `--data-dir`.

**What makes it stay closed.** `tests/integration/test_deployment_isolation.py` pins both
halves: a different deployment never inherits state, and the **same** deployment always does,
because restart recovery is a requirement rather than an accident. Testing only the first
would leave the obvious over-correction — wiping state on start — passing.

**What it cost, deliberately kept.** A pre-existing `data/events.db` from before this change
is no longer read. It is not deleted, and start-up says so once rather than leaving an empty
alert feed unexplained.

### FM-050 — The testbed radar's link parameters and frame layout are unverified
**Status:** Open · **Severity:** High for anyone trusting its output · Blocks run mode LIVE

Published sources disagree about the unit's baud rate (115200 versus 256000) and about whether
the report payload opens with a target count or with an alarm byte followed by a count. The
vendor's protocol PDF has not been obtained. A wrong baud rate does not fail loudly: it
delivers a byte stream that never frames, which without care looks exactly like a working
sensor watching an empty sky (FM-010 one layer lower down).

Separately, the report frame carries no timestamp, so detections are stamped at read time. That
is receipt time, later than observation by the link and buffering latency — bounded by one
report period, but not zero (FM-012).

**Mitigation.** The parser accepts both payload layouts while the question is open, and counts
what it discards. `health()` reports DEGRADED and names the configured baud rate when the
stream is all discards, so the most likely bring-up mistake is visible rather than silent. The
bring-up sequence in the package README ends the ambiguity: confirm the rate, obtain the PDF,
delete the layout branch that is wrong.

**Until then:** raw logging is the deliverable, not detections.

### FM-051 — The testbed radar cannot measure elevation
**Status:** Accepted · **Severity:** Inherent to the hardware

There is no elevation channel and no configuration that produces one. Elevation exists only as
the mechanical tilt of the mount, which rotates the whole ±20° cone — trading ground coverage
for elevated coverage. The canonical `Detection` requires an altitude, so one must be produced.

**Mitigation.** The adapter emits the beam-centre altitude implied by `tilt_deg`, and a
`position_covariance_enu` whose vertical variance is the beam's full elevation ambiguity
treated as a uniform spread. At every range that variance is several times the horizontal, so
the filter discounts altitude rather than believing it. Asserted by
`test_altitude_uncertainty_says_the_sensor_cannot_measure_it`.

Collapsing this into the isotropic scalar would make the tracker confidently wrong about
height, which is FM-004 arriving through the adapter instead of through a datum mismatch.

**What remains open:** altitude from this sensor is a configured assumption, not a measurement,
and no amount of tracking makes it one. Anything downstream that reasons about height — zone
crossing in the vertical, climb rate — is reasoning about the mount angle.

### FM-052 — The testbed radar's sensitivity is tuned for vehicles
**Status:** Open · **Severity:** Unknown, and that is the point

The firmware is vehicle-detection firmware and its 100 m rating is against vehicle-scale radar
cross-section. Whether it detects a 135 g, 25 cm airframe at any range is **unverified**. It
may not. The honest answer today is that nobody has measured it.

**Mitigation.** The adapter emits no classification hint at all, so nothing downstream receives
a label this hardware has no basis for, and confidence derived from the signal-quality byte is
capped well below 1.0. Both are asserted by test.

**What closes this:** the raw-logging test matrix in the package README — bare versus
RCS-boosted airframe, four ranges, three angles, three motion types, three mount tilts — held
next to a record of what was actually flying. Detection and alerting behaviour written before
that dataset exists is guesswork dressed as engineering.
