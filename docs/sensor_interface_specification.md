# Aetos sensor interface specification

**Current version: 1.0.0** · Companion to [`detection_schema.md`](detection_schema.md) ·
Decision record: [ADR 0017](adr/0017-sensor-capability-declaration.md)

This is the document a sensor vendor, a systems integrator, or a customer's engineer reads to
answer one question:

> What does my sensor have to do to work with Aetos?

`detection_schema.md` defines the *shape* a detection must have. This defines the *quality* a
source must reach, what it must declare about itself, and what Aetos does when it falls short.

Living document. Changes here are changes to the contract.

---

## 1. The principle this rests on

**Aetos is transparent to sensor quality.**

The system adds no error of its own and removes none. Every stage propagates the uncertainty it
was given rather than discarding it, rounding it away, or replacing it with a convenient
assumption: the association gate is sized from the reported covariance, the filter weights each
update by it, fusion reconciles by it, and risk is scored on the result. The operator picture is
therefore exactly as good as the sensors feeding it — no better, and no worse.

Two consequences follow, and they are the whole commercial argument:

- **Better sensors improve the picture immediately, with no code change.** A customer who
  upgrades one radar gets the benefit that afternoon. Quality scales with the installation, not
  with the software release.
- **Worse sensors degrade it visibly rather than silently.** A source that cannot support a
  confirmed track does not produce flickering half-tracks the operator has to interpret; it
  produces a stated, declared limitation.

Which is why the floor below is not an accuracy requirement. It is an **honesty** requirement.

> Aetos does not require a good sensor. It requires a sensor that tells the truth about how
> good it is.

A cheap radar with an honest 15-metre error is usable: the filter widens its gate, fusion
weights it lightly, the operator sees a larger uncertainty ellipse, and everything downstream
behaves correctly. A sensor that claims 2 metres and delivers 15 is *worse than no sensor at
all*, because the filter believes it — it will pull a track off a real object, capture a
neighbour's detection through an over-tight gate, and swap two identities with high confidence.

This is already the adapter's stated obligation in `detection_schema.md` ("an adapter that
understates uncertainty makes the filter overconfident, which is worse than reporting
nothing"). This document is what makes that obligation checkable.

---

## 2. What Aetos supplies, and what the sensor must

The division of labour is fixed, and everything peculiar to a device stops at the adapter.

| Aetos supplies | The sensor (via its adapter) must supply |
|---|---|
| Association, filtering, track lifecycle, prediction | Positions, and honest uncertainty on them |
| Multi-sensor fusion | A timestamp of when the observation was **made** |
| Classification from pooled evidence | Its own coverage, so silence can be interpreted |
| Zone geometry, risk scoring, recommendation | A health self-report, always with a reason |
| The event log, replay, the operator interface | Conversion to WGS-84 and SI, whatever it speaks natively |
| Coordinate conversion from local ENU to geodetic | Nothing else |

An adapter is the only place in the codebase permitted to know about a protocol, a wire format,
a native coordinate frame, an altitude datum, a vendor, or a model number. Everything past
`SensorAdapter.get_detections()` reads canonical `Detection` objects and cannot tell what
produced them — the governing principle in `architecture.md`, enforced by the provenance guard
test (ADR 0002).

---

## 3. The floors

Three ordinal floors and one independent grade. A source occupies the highest floor whose
requirements it meets; floors are cumulative, so Floor 2 implies Floor 1 implies Floor 0.

| | Floor | What the customer gets | What it cannot do |
|---|---|---|---|
| **0** | **INGESTIBLE** | Appears on the operator picture. Contributes presence and evidence. Contributes to a track when another sensor is also watching. | Sustain a track on its own. |
| **1** | **TRACKABLE** | Forms and holds confirmed tracks unaided. A single-sensor site works. | Be reconciled reliably against other sensors. |
| **2** | **FUSABLE** | Reconciles with other sensors watching the same object. Multi-sensor sites gain accuracy rather than duplicate tracks. | — |

| | Grade | What it adds |
|---|---|---|
| **C** | **CLASSIFYING** | Contributes evidence about *what* an object is, not only where. Independent of floor: a Floor 0 source may classify, and a Floor 2 source need not. |

**Why classification is a grade and not a rung.** Identity evidence and position quality are
genuinely orthogonal. A fixed camera with a vision model may localise poorly and classify well;
a ranging radar localises well and cannot classify at all. Aetos requires the classification
module to produce a defensible label with no sensor hint whatsoever (ADR 0002), so this grade is
optional forever and most sensors will never hold it.

**Below Floor 0 there is no admission.** A source that cannot produce a valid canonical
`Detection` with an honest uncertainty and a real observation timestamp is not integrated at
reduced capability — it is rejected at the adapter boundary. There is no "best effort" tier,
because a best-effort position with an invented uncertainty is the exact input that corrupts a
tracker.

---

## 4. Requirements

Each requirement states what is required, why Aetos needs it, and how conformance is checked.
Checks marked **automated** are executed by `sensors/adapters/conformance/`.

### Floor 0 — INGESTIBLE

**R0.1 — Canonical detections only.**
Every emitted object is a valid `Detection` at a declared `schema_version`. Validation failures
are the adapter's problem, never the pipeline's.
*Why:* the narrow waist. A source that emits a variant shape forces every downstream module to
learn about it, and the interchangeability the architecture rests on is gone.
*Checked:* automated — every detection is validated, and `extra="forbid"` rejects unknown fields.

**R0.2 — WGS-84, ellipsoidal altitude, SI units.**
Whatever the device speaks natively — range/bearing, a local grid, MSL or AGL altitude, feet,
knots — the adapter converts. Where the source does not measure altitude at all, it declares
that (`reports_altitude = false`) rather than substituting a plausible figure silently.
*Why:* FM-004. Altitude datum confusion is a silent error of tens to hundreds of metres, and it
is invisible until something is scored against a zone floor.
*Checked:* automated for range and datum declaration; the conversion itself is the adapter's
documented obligation and is checked by the adapter's own tests.

**R0.3 — Honest uncertainty.**
`position_uncertainty_m` is a one-sigma value that is **not smaller** than the source's true
error. Where the error is anisotropic by more than about 2:1 — which is true of nearly every
ranging sensor — `position_covariance_enu` is populated and describes the actual error ellipse.
*Why:* the filter uses this as the Kalman gain. Understating it is the single most damaging
thing an adapter can do, and it is damaging in a way that looks like confidence.
*Checked:* automated for presence, positive-definiteness, symmetry, and consistency with the
declared figure. Whether the declared figure is *true* is established by measurement (§8), not
by test.

**R0.4 — Observation time, from the injected clock.**
`timestamp` is when the observation was made — not when the frame arrived, not when it was
parsed. Timezone-aware UTC. Sourced from the injected `Clock`, never from `datetime.now()`.
*Why:* ADR 0003 and FM-012. Receipt time is observation time plus an unknown, variable transport
delay, and that delay becomes position error at target speed.
*Checked:* automated — timestamps must be timezone-aware, must not lead the clock, and
`tests/unit/test_module_boundaries.py` fails the build on any wall-clock call.

**R0.5 — Declared coverage, and nothing outside it.**
The adapter declares range limits, azimuth arc and elevation bounds, and emits no detection
outside them.
*Why:* FM-010. Without coverage, silence from a narrow-beam sensor is indistinguishable from an
empty sky, and the system's worst failure is a confident display over dead data.
*Checked:* automated — every emitted detection is tested against the declared coverage.

**R0.6 — A health record, always with a reason.**
`health()` reports facts: state, last detection time, count, and a human-readable reason that is
populated even when the state is OK. The adapter never decides whether its own silence means a
fault — that is site policy, and it belongs to `backend/system_health`.
*Why:* the threshold that is right for a 10 Hz imaging sensor is wrong for a 1 Hz RF sensor. An
adapter that judged its own silence would encode a policy it cannot know.
*Checked:* automated.

**R0.7 — Lifecycle discipline.**
`start()` and `stop()` are idempotent. `get_detections()` before `start()` raises. After
`stop()` it returns nothing.
*Why:* an adapter nobody started that returns an empty list is a wiring error disguised as an
empty sky.
*Checked:* automated.

**R0.8 — Truthful provenance and sensor type.**
`sensor_type` reflects what the source actually is, and a simulated source never presents itself
as a real one. Provenance markers on optional blocks record how a value was produced.
*Why:* ADR 0002 and ADR 0012. Simulated-versus-real is carried by `sensor_type`, which is the
correct channel for it; provenance is about production, not origin.
*Checked:* automated.

### Floor 1 — TRACKABLE

Requires Floor 0.

**R1.1 — Update rate above the lifecycle floor.**
The source's revisit interval on any bearing within its coverage must satisfy

```
update_period_s  ≤  tentative_max_misses × pipeline_tick_s
```

which is **1.0 s (1.0 Hz)** at current tuning. See §5 for the derivation and why this is a
harder constraint than it looks.
*Why:* a track that cannot reach `confirm_after_detections` before it is retired never becomes
CONFIRMED, and an operator sees a stream of appearing-and-vanishing tentative tracks instead of
one object.
*Checked:* automated, computed against the deployment's actual tuning.

**R1.2 — Position uncertainty below the discrimination ceiling.**
One-sigma position uncertainty at maximum declared range must satisfy

```
σ  ≤  minimum_separation_m / (2 × gate_radius_sigmas)
```

which is **7.4 m** for a 50 m minimum separation at current tuning (§5).
*Why:* association gates are sized from the covariance. Once two objects' gates overlap, the
assignment that keeps them apart is no longer statistically determined and identities swap —
FM-020, and the failure an operator is least able to detect.
*Checked:* automated, computed against the deployment's configured separation.

**R1.3 — Bounded latency.**
End-to-end latency from observation to `get_detections()` must not exceed one update period.
*Why:* latency beyond one update period means the picture is more than one update stale at all
times, and prediction is extrapolating from a position that has already been superseded.
*Checked:* declared, and measurable at integration.

**R1.4 — Stable sensor identity.**
`sensor_id` is stable across restarts and reconnections.
*Why:* fusion, health and the event log all key on it. A sensor that renames itself on
reconnection appears as a new source with no history.
*Checked:* automated across a stop/start cycle.

### Floor 2 — FUSABLE

Requires Floor 1.

**R2.1 — Timestamp accuracy within the motion budget.**
Timestamp uncertainty must satisfy `timestamp_uncertainty × design_speed ≪ σ_position`, and in
any case **≤ 100 ms** (§5).
*Why:* FM-012 and FM-023. Two sensors whose clocks disagree by more than the target moves
between updates place the same object in two places, and fusion produces two tracks rather than
one. Unlike random error, skew is systematic and does not average out.
*Checked:* declared; verified at integration against a common time reference.

**R2.2 — Full covariance where the error is anisotropic.**
A source whose worst-axis and best-axis one-sigma differ by more than 2:1 must populate
`position_covariance_enu`.
*Why:* fusion weights by covariance. A ranging sensor collapsed to an isotropic scalar
contributes its poor cross-range accuracy to the range axis it was actually good at, and the
fused position is worse than either input.
*Checked:* automated — the declared anisotropy ratio is compared against what is emitted.

**R2.3 — A declared, converted datum.**
The source's native altitude datum is declared, and the conversion to WGS-84 ellipsoidal is
implemented and tested in the adapter.
*Why:* FM-004, again, and it is worth stating twice.
*Checked:* declared; the conversion is checked by the adapter's own tests.

### Grade C — CLASSIFYING

Independent of floor.

**RC.1 — Labels from the closed vocabulary.** `ClassificationLabel`, no extensions.
**RC.2 — Calibrated confidence,** reported separately from the label, never folded into it.
**RC.3 — Provenance `measured`,** and the label must be the sensor's own determination.
**RC.4 — Never ground truth.** A simulated source's label is a draw against a confusion model,
never a copy of the truth — a simulated classifier that reports the true label makes the
classification module a passthrough and its entire test suite meaningless (ADR 0002).
*Checked:* automated for RC.1–RC.3; RC.4 is enforced by `ClassificationConfusion`, which refuses
a `correct_probability` of 1.0.

---

## 5. Where the numbers come from

Every figure in §4 is derived from a constant that exists in the code, not chosen for
roundness. If the tuning changes, these change with it, and the conformance suite recomputes
them rather than asserting the value written here.

### D1 — The update-rate floor is 1.0 Hz, and it is stricter than the coast window suggests

Two different lifecycle rules apply at two different stages, and reading only the friendlier one
is a trap worth naming explicitly.

A **confirmed** track coasts on `coast_seconds = 6.0` of *elapsed time*. That is generous, and
it is what carries a track through a brief dropout instead of fragmenting it (FM-022).

A **tentative** track is retired after `tentative_max_misses = 2` *missed cycles* — counted in
cycles, not seconds, deliberately, because what disqualifies a tentative track is a lack of
corroboration rather than the passage of time (`backend/tracking/track_manager/manager.py`).

So, with the pipeline stepping every `DEFAULT_TICK_S = 0.5` s:

```
misses between updates  =  ceil(update_period / tick) − 1
retired when            =  misses ≥ 2
therefore               =  update_period ≤ 2 × 0.5 s  =  1.0 s
```

**A source slower than 1 Hz can never produce a confirmed track**, no matter how accurate it is
and no matter how long it observes: it needs `confirm_after_detections = 4` corroborating
detections, and it is retired after the second missed cycle every time.

Note the asymmetry this creates, because it is genuinely useful. A source meeting 1 Hz while a
track is forming may afterwards drop to gaps of up to 6 s without losing it. **Forming a track
is roughly six times harder than holding one.**

This is the constraint that decides whether a mechanically swept sensor is viable, and it is
much tighter than the coast window alone implies. A sweep with a 3 s revisit will hold tracks
handed to it and will never originate one.

### D2 — The uncertainty ceiling is 7.4 m at 50 m separation

The association gate admits a chi-squared probability mass of `gate_confidence = 0.99` over
`MEASUREMENT_DOF = 3`:

```
gate_threshold   =  chi2.ppf(0.99, 3)   =  11.3449
gate_radius      =  sqrt(11.3449)       =  3.368 σ
```

For two objects separated by `d` to remain distinguishable, neither one's gate may reach the
other's detection, so each gate radius must stay inside half the separation:

```
3.368 σ  <  d / 2      →      σ  <  d / 6.736
```

| Minimum separation | σ ceiling |
|---|---|
| 25 m — tight formation | 3.7 m |
| **50 m — loose formation (default assumption)** | **7.4 m** |
| 100 m — independent targets | 14.8 m |

The 50 m default is a stated site assumption, not a derived fact, and it is the one number here
a deployment should revisit. A site whose threat model is a single intruder can accept a much
larger σ; a site expecting coordinated multiples cannot.

### D3 — The timestamp budget is 100 ms

Timing error becomes position error at the target's speed, and it is systematic rather than
random, so it does not average away across updates:

| Design speed | Displacement per 100 ms | As a fraction of a 7.4 m σ |
|---|---|---|
| 20 m/s | 2.0 m | 27 % |
| 30 m/s | 3.0 m | 41 % |
| 50 m/s | 5.0 m | 68 % |

100 ms keeps timing a minority contributor to total error at typical small-UAS speeds. Beyond
about 250 ms at 30 m/s it becomes the dominant term, and the sensor's own accuracy stops
mattering.

### Constants these derive from

| Constant | Value | Where |
|---|---|---|
| `gate_confidence` | 0.99 | `backend/tracking/association/gnn.py` |
| `MEASUREMENT_DOF` | 3 | `backend/tracking/association/gnn.py` |
| `confirm_after_detections` | 4 | `backend/tracking/track_manager/manager.py` |
| `tentative_max_misses` | 2 | `backend/tracking/track_manager/manager.py` |
| `coast_seconds` | 6.0 | `backend/tracking/track_manager/manager.py` |
| `DEFAULT_TICK_S` | 0.5 | `backend/main.py` |

---

## 6. Declaring capability

Every adapter reports a `SensorCapabilities` document from `capabilities()`, alongside the
health record it already reports. It is a **static declaration** — what this source is, not what
it is currently doing — so it is cheap to call, stable across a run, and safe to show a customer
before anything is connected.

It is deliberately separate from `SensorHealth`. Capability answers *what can this sensor do*;
health answers *is it doing it right now*. Folding them together would make a declared
specification change when a cable came loose.

```
identity      sensor_id · sensor_type · description · schema_version
coverage      min/max range · azimuth arc · elevation bounds
timing        update_rate_hz · max_latency_ms · timestamp_uncertainty_ms
accuracy      position_uncertainty_m at max range · anisotropy ratio ·
              reports_covariance
dimensions    reports_range · reports_bearing · reports_elevation ·
              reports_altitude · reports_radial_velocity
identity ev.  classifies
transport     transport kind · integrity (see §7)
```

Two fields carry more weight than the rest and are worth calling out.

**`reports_altitude = false` is a first-class answer.** A range-and-bearing sensor with no
elevation channel measures a two-dimensional position. It must still populate `altitude_m`,
because the canonical schema requires it — but the altitude it reports is an *assumption*, and
the declaration is what lets the rest of the system know. The operator interface renders such a
figure as not measured rather than as a number (ADR 0011, and see ADR 0019 for the schema
marker that carries it per-detection). Downstream, the vertical variance in
`position_covariance_enu` must be large enough to say the same thing numerically.

**`anisotropy_ratio` is how a scalar-only source declares that it is lying by simplification.**
A source reporting a scalar uncertainty for an error ellipse that is 10:1 elongated is
technically compliant with Floor 0 and unfit for Floor 2, and this field is what makes that
visible before it is discovered in a fusion bug.

---

## 7. Transport integrity

A declaration Aetos requires that most sensor specifications do not mention, because it is the
difference between a link that fails loudly and one that lies quietly.

Every adapter declares the integrity its transport provides:

| Level | Meaning | Typical |
|---|---|---|
| `verified` | Corruption is detected by a checksum or equivalent, at or below this layer | TCP, TLS, a protocol with a CRC |
| `structural` | No checksum. Corruption is detected only by the frame's own structure and the plausibility of its values | A raw UART protocol with header/length/tail and nothing more |
| `none` | Corruption is undetectable | A protocol with no framing at all |

A `structural` link is admissible, and it is what a great deal of cheap sensor hardware actually
provides. What is **not** admissible is treating it as though it were `verified`. A source on a
structural link must additionally declare:

- **`residual_corruption_probability`** — the computed probability that a single corrupted byte
  produces a frame the decoder accepts as valid. This is a property of the frame layout and the
  configured plausibility envelope, so it is *calculated*, not estimated by hand. See
  `sensors/adapters/ld2451/protocol.py` for a worked implementation.
- **A counted rejection record** in `health()`, broken down by reason. A rising rejection rate is
  the earliest available signal of a failing cable, a wrong baud rate or electrical interference,
  and it must never be silent.

The reasoning is set out at length in [ADR 0018](adr/0018-transport-integrity.md). In short: you
cannot add integrity to a wire that has none, so you measure the wire instead, and you degrade
the source's declared trustworthiness as the measurement worsens. A sensor that is probably
lying is worse than a sensor that is silent, and the system must be able to reach that
conclusion on evidence.

---

## 8. Proving conformance

Conformance is a **test result with a date on it**, not a claim.

```
python -m sensors.adapters.conformance --help
```

The suite in `sensors/adapters/conformance/` takes any object implementing `SensorAdapter`,
exercises it, and reports the floor it reached together with every requirement that failed and
why. It is shipped as part of the product rather than kept in `tests/`, precisely so that a
third party integrating their own sensor can run it against their own adapter without a copy of
our test suite.

What the suite can and cannot establish is worth stating plainly, because a conformance report
that overclaims is the same failure this document is about:

**It can check** structural conformance, lifecycle discipline, coverage adherence, declaration
self-consistency, schema validity, provenance correctness, and whether the *declared* figures
clear the computed floors.

**It cannot check** whether the declared figures are true. No test can determine that a sensor
claiming 3 m accuracy delivers it. That requires measurement against a known reference — a
survey, a GNSS-tracked flight, a calibrated range — and it is an integration activity, not a
software one. The suite therefore reports declared conformance and says so in those words.

**The gap between the two is where the honesty requirement lives**, and it is why §1 is framed
around honesty rather than accuracy. Aetos can enforce that a sensor makes a specific, checkable
claim about itself and behaves consistently with it. It cannot enforce that the claim is honest.
What it can do — and does — is make the claim explicit, attributable, and falsifiable by
measurement, rather than leaving it as an assumption buried in an integration.

---

## 9. What happens when a source falls short

Falling short is a normal, supported condition. It is never silent.

| Situation | What Aetos does |
|---|---|
| Below Floor 0 | Adapter refuses to start, naming the requirement. Nothing is ingested. |
| Floor 0 only | Ingested and displayed. `GET /sensors` reports the floor. Tracks it contributes to are formed by other sources; alone, it produces tentative tracks that do not confirm — which is the honest outcome, not a bug. |
| Floor 1, not 2 | Tracked normally. Fusion treats it with the reconciliation tolerance its declared timestamp uncertainty implies. |
| Declared capability contradicts observed behaviour | The conformance check fails and names both figures. A sensor emitting outside its declared coverage is a declaration error, not a coverage discovery. |
| Transport integrity degrading | Rejection counts rise in `health()`; the adapter reports DEGRADED with the reason; past a configured threshold it reports OFFLINE, because a source that is probably corrupting data is worse than one that is quiet. |
| Coverage never validated against the threat class | Declared `coverage_validated = false`. Silence from that sensor must not be rendered as clear sky. See FM-016. |

---

## 10. Integrating a new sensor

The whole checklist, in order.

1. **Read** `detection_schema.md` for the shape, and this document for the quality.
2. **Write a registry document** in `config/sensor_registry/` with `kind: "live"`, declaring the
   sensor's characteristics and its driver parameters. Characteristics are configuration, never
   code — the same model fitted at two sites must not require two builds.
3. **Implement `SensorAdapter`** in `sensors/adapters/<driver>/`. Four methods plus
   `capabilities()`. Everything vendor-specific stays inside this directory — enforced by
   `tests/unit/test_module_boundaries.py`, which fails the build if a model or manufacturer name
   appears anywhere else in `backend/` or `sensors/`.
4. **Separate the codec from the transport.** The wire-format decoder should be a pure function
   of bytes with no I/O at all, so it can be tested exhaustively — including against corruption
   and desynchronisation — without the hardware present. A parser you can only test against a
   live device is a parser you cannot test.
5. **Run the conformance suite.** Fix what it names.
6. **Add the placement** to the site document in `config/sites/`.
7. **Measure what it can actually see.** Declared coverage from a datasheet is a manufacturer's
   claim about a different target than yours. Fly the threat class at known ranges and bearings,
   record detection probability against range, and replace the declared figure with the measured
   one. Until this is done, `coverage_validated` stays false.

Step 7 is the one most likely to be skipped and the one most likely to matter. A datasheet range
figure is quoted against a specific radar cross-section, and a small multirotor is one to two
orders of magnitude below the target most commercial sensors are rated for.

---

## 11. What this specification deliberately does not require

Stated so that an integrator does not build what is not needed.

- **No particular protocol, transport or wire format.** Serial, TCP, UDP, HTTP, MQTT, ONVIF,
  ADS-B, Remote ID, a vendor SDK, or a file on disk are all equally acceptable. The adapter is
  the only thing that has to know.
- **No minimum range, sensitivity or resolution.** Those determine which floor a source reaches;
  they are not entry requirements.
- **No classification.** Grade C is optional forever.
- **No elevation or altitude channel.** Its absence must be declared. It is not disqualifying.
- **No track output.** Aetos forms its own tracks from detections. A source that produces tracks
  natively should emit them as detections and let the tracker do its job; feeding a second
  tracker's output into this one produces correlated errors neither can account for.
- **No time synchronisation protocol.** PTP, NTP or GNSS discipline are means, not requirements.
  What is required is a declared timestamp uncertainty that is honest, and — for Floor 2 — small.
- **No specific update rate beyond D1.** Faster is better and is not required past the floor.

---

## 12. Version history

| Version | Change |
|---|---|
| 1.0.0 | Initial specification. Three floors, one grade, transport integrity declaration. |
