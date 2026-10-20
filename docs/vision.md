# Aetos — long-term product vision

**Status: aspirational. Nothing in this document is a claim about what the system does
today.** For what exists now, read `README.md` and `docs/architecture.md`; those describe
built software. This document describes where the architecture is heading over years, so that
decisions made this month do not foreclose it.

Read it as a constraint on design, not as a backlog. The value of writing it down is that when
a module boundary is being drawn, there is a shared answer to *"does this shape still work
when we get to fleet-scale, cross-site coordination, or heterogeneous third-party sensors?"*

## Positioning

> Not a radar. Not a jammer. Not an interceptor.
> **Aetos is the software-defined security layer that turns heterogeneous sensors and
> authorised response assets into one intelligent, asset-aware C-UAS network.**

Four properties define it:

**Hardware-agnostic** — it integrates with the sensors a customer already owns.
**Asset-aware** — it protects what actually matters, not airspace uniformly.
**Threat-aware** — it assesses situations continuously, not objects once.
**Response-aware** — it knows the state of the entire defensive system, including what is
unavailable.

## The scope boundary, restated

`README.md` states a hard boundary: Aetos ends at a recommendation to a human, and contains no
effector control. **That boundary survives this document intact.**

Several capabilities below (§8, §9, §10, §12) concern response. None of them relaxes the
boundary, because the thing they add is *knowledge of resources and articulation of options*,
not the ability to act. The distinction that keeps this honest is a three-state separation the
system must make explicit everywhere it appears:

```
AETOS HAS DETECTED  →  AETOS BELIEVES  →  AETOS RECOMMENDS
                                              ↓
                                    OPERATOR AUTHORISES
                                              ↓
                                    SYSTEM EXECUTES / COORDINATES
```

`RECOMMENDED`, `AUTHORISED` and `EXECUTED` are three different states with three different
authors. Aetos may author the first. A human authors the second. Only after both does anything
downstream of this repository author the third. An operator must never have to wonder whether
Aetos has already done something.

Any code that crosses into execution goes through the design pass described in
`response/README.md` — authorisation, safety, audit — done deliberately and separately. Adding
capabilities from §8–§10 to *this* codebase means adding decision support about response, and
nothing else.

## The philosophy

Every capability below serves one of twelve questions. When a proposed feature answers none of
them, it is probably not an Aetos feature.

| | Question |
|---|---|
| **SEE** | Can Aetos sense the environment? |
| **FUSE** | Can it understand that multiple observations are the same object? |
| **IDENTIFY** | What is the object? |
| **UNDERSTAND** | What is it doing? |
| **PROTECT** | What assets are actually at risk? |
| **ASSESS** | How dangerous is the situation? |
| **RESPOND** | What is the lowest-risk effective response? |
| **COORDINATE** | What resources are available? |
| **ADAPT** | How does it behave when sensors, power, weather or comms fail? |
| **LEARN** | What has happened historically? |
| **SCALE** | Can the same architecture protect one site, twenty, or a network? |
| **PROVE** | Can it show exactly what happened, and why? |

`PROVE` is the one that is easy to drop and expensive to retrofit. Aetos should not merely
assert that it protects a site; it should be able to demonstrate its coverage, health, threat
history and performance on demand.

---

## Status legend

Each capability carries a status against the code in this repository as of this writing.

- **Built** — implemented and tested.
- **Partial** — a working core exists; the capability as described here does not.
- **Placeholder** — the module exists and is empty, or the shape is reserved.
- **Not started** — no code.

---

# SEE

## 1. Aetos Sense — hardware-agnostic sensor integration

*Status: partial. `sensors/adapters/interface.py` defines the `SensorAdapter` contract; a
simulated adapter and one real driver implement it — a serial FMCW radar (ADR 0017), selected
by run mode (ADR 0018). That radar's detection performance has not been measured.*

A customer should not have to replace working radar in order to buy Aetos. The target
integration set is: existing radar, Aetos-compatible radar, EO cameras, thermal cameras, RF
sensors, acoustic sensors, ADS-B, Remote ID where applicable, existing CCTV, and other site
sensors.

That is not unconditional. Anything can connect *provided it meets the Aetos sensor floor
specification* — a published minimum covering:

- data format
- position accuracy
- timestamp accuracy
- update rate
- maximum latency
- range reporting
- bearing reporting
- altitude reporting where available
- sensor health reporting
- network/API compatibility

The floor spec is what makes "connect your existing infrastructure" a commercial promise
rather than a support liability. It is also the natural extension point of the governing
principle already enforced by ADR 0002: the tracking engine cannot tell where a detection came
from, so a third-party radar that meets the floor is indistinguishable from a first-party one.

The floor spec should become a document in `docs/` and a validation suite before any real
adapter is written.

---

# FUSE

## 2. Aetos Fusion — one object, however many sensors see it

*Status: partial. `backend/fusion/` reconciles several sensors' detections of one object into
one measurement before tracking (ADR 0020), and each track lists the sensors behind it. There is
no object-confidence figure distinct from sensor confidence yet — the idea below.*

Four sensors observing one drone must produce one track, not four objects:

```
TRACK 017
  Classification        UAV
  Confidence            94%
  Sensors contributing  4
  Range                 489 m
  Altitude              71 m
  Velocity              12.8 m/s
  Heading               218°
```

The load-bearing idea:

> **Sensor confidence ≠ object confidence.**

Radar may be 90% sure it sees *something*; the camera 80%. Aetos may nonetheless be 97%
confident that all four observations are one physical object. Deriving the second number from
the first is the difference between a sensor network and a sensor-fusion system — and it is
the piece a customer cannot assemble themselves from the boxes they already own.

---

# IDENTIFY

## 3. Aetos Identify — three separate questions

*Status: partial. `backend/classification/` answers "what is it" by weighted evidence
(ADR 0012). The other two questions have no implementation.*

Classification is not one question. It is three, and collapsing them loses the answer.

**What is it?** — bird, UAV, aircraft, helicopter, balloon, unknown, other.

**Who or what does it belong to?** — registered/authorised, known, unknown.

**What is it doing?** — passing through, approaching, loitering, surveying, departing,
repeatedly probing, moving toward a protected asset, coordinating with other tracks.

Keeping them separate is what allows the correct conclusions:

- An unknown aircraft is not automatically a threat.
- An authorised drone is not automatically safe.
- A *known* drone behaving abnormally may be the most interesting track on the screen.

---

# UNDERSTAND

## 4. Behavioural intelligence

*Status: not started.*

Aetos continuously analyses trajectory, speed, altitude, acceleration, loitering, repeated
approaches, perimeter probing, unusual flight paths, approach toward sensitive assets, sudden
direction changes, multiple coordinated tracks, time spent inside protected zones, and
historical behaviour.

The output difference is the whole point. Instead of:

```
DRONE DETECTED
```

the operator gets:

```
UAV DETECTED
  Behaviour              unusual
  Trajectory             intersects protected asset
  Identity               none authorised
  Threat assessment      increasing
```

---

# PROTECT

## 5. Aetos Digital Twin

*Status: partial. `twin/` is a standalone 3-D prototype: a demonstration site, protected zones
as volumes, simulated tracks, an asset-aware threat model and a site editor, running on its own
simulator. It is not yet wired to the Aetos backend, and `backend/zones/` remains 2-D.*

On installation, Aetos builds a 3-D digital representation of the protected facility. For a
solar site:

| Zone | Contents | Risk |
|---|---|---|
| A | Solar array | Medium |
| B | Inverter station | High |
| C | Substation | Critical |
| D | Control building | Critical |
| E | Perimeter | Low |

The operator sees the actual site, not an abstract range ring. More importantly, so does the
threat engine: the statement changes from *"the object is 300 m away"* to *"the object is
300 m out and its predicted trajectory intersects the critical substation zone."* Those are
different situations with the same range.

This is a signature feature, and it is the natural consumer of zone-crossing prediction that
`backend/zones/` already exists to provide.

## 6. Asset-aware protection

*Status: partial. Sites and zones are configurable under `config/sites/`; per-asset criticality
is not modelled.*

> Do not protect empty airspace equally. Protect what actually matters.

Customers define assets with criticality and protection geometry:

| Asset | Criticality | Note |
|---|---|---|
| Transformer 01 | 10/10 | Dedicated protection zone |
| Solar panels | 5/10 | Broad area, low per-unit value |
| Public road | — | Different risk *category*, not a lower score |

The last row matters: some assets change the risk calculus rather than sitting lower on it. A
public road near the site is a collateral-risk consideration, not a thing to be defended.

---

# ASSESS

## 7. Aetos Threat Engine

*Status: partial. `backend/risk/` scores tracks with reason codes and hysteresis (ADR 0013,
ADR 0007). The dimensions below are not all modelled.*

Threat is multidimensional and continuous. Inputs:

object confidence · intent/behaviour · proximity · velocity · trajectory · target criticality ·
flight-restriction status · historical behaviour · number of simultaneous UAVs · sensor
confidence · environmental conditions

→ **dynamic threat score**

The score must keep moving:

```
t+0s    Threat 21 — LOW
t+10s   Threat 46 — MODERATE
t+18s   Threat 78 — HIGH     ← trajectory turned toward a critical asset
```

Assigning a threat level once and leaving it there is the failure mode this replaces. Note the
tension with hysteresis (ADR 0007): the score should be responsive, the *level* should not
chatter. That is a solved problem in the current engine and the solution must survive the
extra dimensions.

---

# RESPOND

Everything in this section is decision support. See "The scope boundary, restated" above.

## 8. Aetos Response Engine

*Status: partial. `backend/decision/recommend.py` emits a recommendation category. It does not
reason about resources, conditions or constraints.*

The question the engine asks is:

> What is the safest effective response to this situation?

**not**

> What countermeasure can we fire?

It weighs threat level, confidence, asset criticality, available resources, environmental
conditions, legal and operational constraints, collateral risk, and whether the threat is
actually confirmed — and recommends the lowest-risk *effective* response.

## 9. Aetos Response Hierarchy

*Status: partial. The recommendation ladder `MONITOR → VERIFY → ALERT → ESCALATE →
RESPONSE_RECOMMENDED` already encodes escalation; the reasoning behind rung selection is
thinner than described here.*

A formal design principle: **lowest-risk effective response**. Aetos never jumps to the most
aggressive option available. It asks, in order:

1. Can this be resolved by identification? → identify.
2. Can it be handled by monitoring and alerting? → monitor.
3. Does it genuinely require intervention? → recommend an appropriate authorised response.
4. Is the threat escalating? → escalate accordingly.

This is what makes Aetos a risk-management system rather than a weapons controller.

## 10. Aetos Interceptor Hub

*Status: not started.*

Each site maintains a live inventory of its authorised defensive assets:

```
INTERCEPTOR 01
  Status            Available
  Location          Site East
  Battery           87%
  Condition         Excellent
  Operating envelope Available
  Readiness         Ready
  Communications    Connected
  Current task      None
```

The value is knowing *before* an incident. The failure this prevents is discovering mid-event
that an asset has been unserviceable for a fortnight.

## 11. Response resource management

*Status: not started.*

Combine the hub with the threat engine and Aetos can reason about allocation:

```
Threat 021 — high priority
  Asset 03   available
  Asset 04   unavailable
  Asset 05   maintenance
```

and recommend which resource suits the situation under predefined rules. To be explicit: the
feature is **not autonomous weapon control**. It is that *Aetos knows the state of the entire
defensive system* and can say so.

---

# COORDINATE

## 12. Aetos Network — nodes and core

*Status: not started. Aetos is currently a single process.*

Enormous facilities do not need one enormous system. They need sectors:

```
AETOS NODE 01 (north) ─┐
AETOS NODE 02 (east)  ─┼─→ AETOS CORE  ── unified air picture
AETOS NODE 03 (south) ─┤
AETOS NODE 04 (west)  ─┘
```

Core maintains the single air picture. If Node 02 sees an object, Node 03 must not conclude it
is a different object — cross-node fusion is the same problem as §2 with worse clocks and a
network in the middle. The network shares tracks, sensor data, threat information, site zones,
asset information, system health and events.

## 13. Cross-site Aetos

*Status: not started.*

A site under significant pressure is not an island. Aetos Core can query the wider authorised
network:

```
SITE A  ── resource request ──→  SITE B   2 available authorised assets
                                 SITE C   1 available authorised asset
                                 SITE D   none available
```

Core coordinates the request and the availability picture, subject to whatever authorisation
and operational rules govern the participating sites. This turns site protection into
**distributed regional protection infrastructure** — a substantially larger proposition, and
one with correspondingly larger authorisation questions to answer first.

## 14. Aetos Interoperability — the API as product

*Status: partial. `backend/api/` serves REST and WebSocket for the Aetos frontend; it is not
designed as an integration surface for third-party systems.*

Aetos must not exist in isolation. It should talk to CCTV, security systems, control rooms,
access control, emergency management, existing radar, site-management systems, authorised
defensive systems, and other Aetos sites.

Another system should be able to ask: *current airspace picture? · any high-threat tracks? ·
is Site 07 operational?* An explicit, documented, versioned Aetos API is what makes Aetos an
infrastructure platform rather than another proprietary box.

---

# ADAPT

## 15. Aetos Self-Awareness

*Status: partial. `backend/system_health/` judges every sensor against site thresholds,
declares DEGRADED or BLIND posture, and detects a stalled pipeline (ADR 0019). It names the
sources that are out; there is no readiness percentage and no recommended maintenance action.*

Aetos monitors Aetos, continuously:

```
Radar 01      98%      Network       99%
Radar 02     100%      Power        100%
Camera 01     94%      Core compute  97%
Thermal 01    81%
RF sensor  OFFLINE

AETOS READINESS  91%
```

But a number alone is not useful. The output must be diagnostic:

> Primary weakness: southwest radar coverage degraded.
> Recommended action: inspect Node 03.

That is worth more to an operator than any green/red light.

## 16. Aetos Coverage Map

*Status: not started.*

Aetos should know where it *cannot* see, and say so — strong coverage, reduced confidence,
detection gap — updating dynamically as sensors degrade or fail. On a large site this is among
the most valuable things the system can display, because it answers the question a customer
actually has:

> "Where am I vulnerable?"

## 17. Weather intelligence

*Status: not started.*

Do not merely display weather. Model its effect on sensor confidence:

```
Heavy rain detected
  Camera confidence    ↓
  Thermal confidence   →
  Radar confidence     →
  → object confidence adjusted accordingly
```

The system should understand *"I have less visual certainty right now"* rather than pretending
conditions are nominal. Each deployment is designed and tested against its own environmental
envelope: heavy rain, fog, strong wind, temperature extremes, icing, dust.

## 18. Rapid deployment

*Status: not started.*

An Aetos Rapid Deployment Kit: open case → deploy node → mount → connect power → connect
network → select site profile → Aetos calibrates → system health check → operational.

Target: **case to operational in hours, not weeks.** Compelling for temporary infrastructure,
events, and emergency deployment.

## 19. Power-outage and disconnection mode

*Status: not started.*

Essential for energy infrastructure. Local resilience is the rule:

- Site loses external power → Core continues locally.
- Internet disappears → Core continues locally.
- Central cloud disappears → Core continues locally.
- Connectivity returns → `Synchronising 17 events…`

**The cloud is not the brain.** Every site has its own local Aetos Core, and the event log
(ADR 0006) is the substrate that makes deferred synchronisation tractable.

## 20. Aetos cybersecurity

*Status: partial. Authentication, three roles and an audit trail exist (ADR 0016). The rest
does not.*

Formalised as:

- **Zero-trust architecture** — every device authenticates.
- **Encrypted communications** — sensor → node → core.
- **Device authentication** — a rogue sensor cannot simply join the network.
- **Role-based access** — different operators, different permissions.
- **Secure updates** — authenticated, signed software.
- **Network segmentation** — one compromised subsystem does not expose everything.
- **Anomaly detection** — Aetos watches its own network for suspicious behaviour.
- **Audit logs** — everything important is recorded.
- **Offline operation** — network failure is not security failure.

The governing principle:

> **Compromise of one sensor must not equal compromise of Aetos.**

---

# LEARN

## 21. Aetos Black Box

*Status: partial — and the strongest foundation already in place. `backend/events/` is an
append-only durable log and is the system's source of truth (ADR 0006), with replay.*

Every event produces a chronological record:

```
14:32:01  Radar detects object
14:32:02  Second sensor detects object
14:32:03  Fusion engine correlates observations
14:32:05  Object classified as probable UAV
14:32:07  No authorised identity found
14:32:11  Trajectory intersects restricted zone
14:32:12  Threat escalated
14:32:13  Operator notified
14:32:17  Response recommendation generated
14:32:25  Operator action recorded
```

After an incident it must be possible to reconstruct: what did Aetos see, what did it believe,
why did the threat score change, what did it recommend, and what did the operator do. This is
the `PROVE` pillar, and it is the reason the event log is the source of truth rather than a
convenience.

## 22. Aetos Knowledge Base

*Status: not started.*

Aetos learns what normal looks like at each site: common drone types, common aircraft, normal
flight paths, normal times, typical weather, false-positive sources, sensor performance,
previous incidents, threat patterns.

Knowing normal is what makes abnormal cheap to detect.

## 23. Aetos Historical Intelligence

*Status: not started.*

Operators should be able to ask:

- *"Show me every UAV detection in the last 90 days."*
- *"Show me unusual activity around the substation."*
- *"Which sensor has generated the most false positives?"*
- *"What time of day are UAV detections most common?"*

At that point Aetos is not only defending the site in real time — it is helping a security
team understand their site over months and years.

## 24. Aetos Simulator

*Status: built, and ahead of the rest of the system. `scenarios/` provides nine named scenarios
including clutter, degraded sensors, ambiguity and sensor disagreement.*

The simulator should eventually cover one UAV, multiple UAVs, swarms, birds, aircraft, false
positives, sensor failures, weather degradation, communications failures, power failure, and
multiple simultaneous incidents.

Its strategic value is demonstration: it shows what Aetos *would* do without requiring every
response system to be physically implemented. That is how advanced capability gets
demonstrated honestly — as simulation clearly labelled as simulation.

---

# SCALE

## 25. Aetos Fleet / Aetos Command

*Status: not started.*

An operator with twelve solar farms should not have twelve unrelated systems:

```
AETOS COMMAND — 12/12 sites operational
  Site 01   Normal
  Site 02   2 active tracks
  Site 03   Sensor degradation
  Site 04   Normal
  Site 05   Maintenance
  …
```

with zoom-in to any site. This is an entire SaaS layer above the physical C-UAS system, and
probably a distinct commercial product.

## 26. The bigger Aetos network

*Status: not started.*

```
                   AETOS COMMAND CLOUD
                           │
            ┌──────────────┼──────────────┐
            │              │              │
         SITE A          SITE B         SITE C
            │              │              │
        AETOS CORE     AETOS CORE     AETOS CORE
        /    |    \      / | \          / | \
      N1     N2    N3   N1 N2 N3      N1 N2 N3
```

Every site retains local autonomy. The wider network adds fleet visibility, threat
intelligence, resource availability, cross-site awareness, maintenance information, software
updates and historical analysis.

Crucially: **loss of the cloud does not disable local protection.** See §19.

---

# PROVE (operator-facing)

## 27. Aetos human-authority model

*Status: partial. The recommendation ladder ends at the operator by design; the UI does not
yet express the authorise/execute states because nothing can execute.*

The interface must make the chain unmistakable — `HAS DETECTED → BELIEVES → RECOMMENDS →
OPERATOR AUTHORISES → SYSTEM EXECUTES`. See "The scope boundary, restated".

## 28. Aetos Command UI

*Status: partial. `frontend/` presents a PPI display, tracks, classification, risk and alerts.*

A trained security operator should understand the situation within seconds:

```
AIRSPACE   14 active tracks        SITE      97% coverage
THREATS    1 HIGH · 2 MED · 11 LOW SYSTEM    94% readiness
                                   ASSETS    5 available
```

and, for the event that matters:

```
THREAT 018 — HIGH
  Probable UAV · 421 m
  Approaching critical substation
  Confidence 94%
  ETA to protected zone  00:38
  Recommended action     [ REVIEW ]
```

Everything important, immediately visible.

## 29. Aetos Ask — natural-language interrogation

*Status: not started.*

The operator should be able to ask the system directly.

> **"What's happening?"**
> Three unidentified UAV tracks are active. One is approaching the eastern perimeter. Two are
> moving away. No critical assets are currently threatened.

> **"Why is Track 18 high?"**
> Track 18 is unidentified, has entered a restricted zone, and its projected trajectory
> intersects the critical substation.

> **"Are we fully operational?"**
> No. Southwest radar coverage is degraded and one thermal camera is offline. Estimated
> coverage is 82% in that sector.

Note that every one of those answers is drawn from data other capabilities already produce.
Ask is an interface onto §7, §15 and §16 — not a separate intelligence.

## 30. Aetos Protection Score

*Status: not started.*

Do not say "system operational". Say:

```
AETOS PROTECTION READINESS  91%
```

derived from sensor availability, coverage, weather, communications, power, detection
confidence, response-resource availability, network health and critical-zone coverage — and
then, as always, the diagnostic:

> Main vulnerability: southwest sector.
> Main recommendation: restore Node 04.

Aetos is not merely telling you there is a threat. It is telling you **how well it can protect
you right now, and where it is weak.**

---

## How to use this document

- When adding a module, check which pillar it serves. If none, question it.
- When drawing an interface, check it does not foreclose §12 (nodes/core), §14 (third-party
  integration) or §25 (fleet). Those three are the ones a single-process assumption quietly
  kills.
- When touching anything near response, re-read the scope boundary and `response/README.md`
  before writing code.
- When a capability here becomes real, record the decision as an ADR under `docs/adr/` and
  update the status line above. This document should never claim more than the code does.
