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
