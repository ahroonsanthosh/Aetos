# ADR 0018 — Run mode selects the adapter set, and refuses what it cannot honour

**Status:** Accepted
**Date:** 2026-08-15
**Builds on:** ADR 0002 (one canonical Detection), ADR 0017 (real hardware behind the existing
adapter contract)

## Context

ADR 0017 added a driver for a real radar and stopped there, deliberately: it was constructed by
nothing and referenced by no configuration. `docs/architecture.md` has described three run
modes since the first commit and said mode selection would be "adapter registry configuration
rather than a code path". Nothing implemented that. `build_application` attached simulated
adapters when a scenario was named and nothing otherwise, and reported `RunMode.SIMULATION`
to the API as a constant.

Making the system usable against real hardware means answering three questions that were open:
where adapters come from, what happens when the configuration cannot satisfy the requested
mode, and what the operator interface is told.

The danger throughout is specific and one-directional. Every failure worth designing against
here ends with **an operator looking at a confident display over data that is not what the
badge above it claims** — synthetic tracks under a LIVE badge, a LIVE badge over a sensor that
never opened its port, or real returns mixed with scenario traffic and nothing saying which is
which.

## Decision

### 1. A placement is simulated or real, and the site document says which

`SensorPlacement` gains an optional `hardware` block and its `registry_key` becomes optional.
Exactly one must be set, enforced by validation.

The alternative was to keep `registry_key` mandatory and point live hardware at a registry
entry. That entry holds probability of detection, false alarm rate and measurement sigmas —
invented numbers, appropriate for a simulator and fabrications about a real device. A reader of
the sensors table would have had no way to tell which rows described a device and which
described a scenario. The mutual exclusion is what makes `--mode live` mean something
checkable instead of aspirational.

### 2. Mode selects adapter families, checked against configuration before anything starts

`resolve_sources(mode, site, scenario)` returns which families to attach, or raises
`ModeRefused`. SIMULATION attaches simulated adapters only, LIVE attaches hardware only, HYBRID
attaches both.

Four refusals, each corresponding to a display that would have lied:

- **LIVE at a site with no hardware placement.** A LIVE badge over an empty sky nothing is
  watching.
- **LIVE with a scenario.** Synthetic tracks in a live picture. This is HYBRID, and it must be
  asked for by name so that whoever reads the screen knows both are present.
- **HYBRID missing either half.** A label that describes something other than what is running.
- **A scenario at a site with no simulated sensors.** It would drive nothing, and the empty
  display would have no visible cause.

Refusing is not the conservative choice here, it is the honest one: every alternative starts a
system that shows something plausible and wrong.

### 3. The badge follows attachment, not declaration

`GET /system/mode` reports `real_adapters_connected` counted from the adapters the pipeline
actually holds. Declaring LIVE in configuration does not change the badge; attaching a sensor
does. The two are reported side by side rather than folded together, because a deployment
declaring LIVE with nothing attached is a misconfiguration the operator needs to see.

Attached is not the same as *talking*, and the count deliberately does not check. An unplugged
radar keeps the badge on LIVE, because the alternative — falling back to SIMULATION — would
claim everything on screen was synthetic when in fact nothing was on screen at all. Whether a
fitted sensor is currently reporting is a different question, answered in the next decision.

### 4. Sensor liveness is a health row, polled per request

`GET /system/health` gains a `sensors` component, read from every attached adapter's own
`health()` each time it is asked. Any sensor not reporting OK degrades the row, and the row
names which one and why.

Pessimistic on purpose. A site with four sensors and one dead USB cable must not average out to
healthy — the operator's question is never "are most of my sensors fine". The judgement lives
here rather than in the adapters, which report facts and make no claim about what silence means
(FM-010); the threshold that is right for one sensor is wrong for another, and the adapter
cannot know it.

This is the first thing in the system that answers "is my sensor actually working" from
evidence. It is not the watchdog `backend/system_health` will be — there is no rate analysis,
no degraded-mode controller, no automatic action — and it does not claim to be.

### 5. Persistent state is partitioned per deployment

`data/<site>/<mode>/`, with a `deployment.json` marker naming the owner and a start-up refusal
when it disagrees.

This was not in the original design and was added after the mode switch was found to leak: a
live run started after a scenario inherited the scenario's tracks and served them under a LIVE
badge (FM-053). The event log survives restarts by design, and nothing said which deployment a
given history belonged to, so "survives restarts" quietly meant "survives becoming a different
system".

The partition follows from what a log *is*. It is the record of one site operating in one
mode; two of those in one file was never meaningful, and the storage layout now says so. The
marker exists because an explicit `--data-dir` bypasses any defaulting scheme, and a guarantee
that holds only when the path was chosen for you is not a guarantee.

Restart recovery is unaffected and deliberately tested alongside: the same site in the same
mode resolves to the same directory and picks its own history back up, alerts included
(FM-030, ADR 0015). Wiping state on start would have "fixed" the leak by breaking that.

### 6. One writer per deployment, enforced by a lock that fails open

A deployment directory carries an advisory lock, held for the life of the process. A second
Aetos told to run the same site in the same mode is refused, because two processes writing one
event log interleave their records and leave no way to say afterwards which wrote what
(ADR 0006). Different ports do not help — a port stops two servers, not two writers.

It **fails open**: if the platform cannot lock, or locking errors, the run continues with a
warning. The Windows path is written from documentation rather than from a machine, and a lock
mechanism that could refuse a legitimate start on a platform nobody could test would be a worse
defect than the one it prevents.

### 7. The pipeline steps whenever any adapter is attached

Previously the cycle loop ran only when a scenario was named. Live hardware produces detections
because something is in the sky, not because a scenario said so, and a loop gated on scenarios
would have left real sensors reporting into a pipeline nobody was stepping.

### 8. Hardware construction lives in `backend/main.py`

`build_hardware_adapter` is where a site document meets a driver, and it is deliberately dull:
it copies configuration across and constructs one object. It sits in `main.py` because
`sensors/` may not import `backend.zones` — adapters know the canonical schema and the clock,
and nothing about sites, zones or run modes. The one piece of arithmetic it does is adding the
site reference altitude to the mast height, because the driver works in metres above the
ellipsoid and `height_m` is measured from the site reference (FM-004).

### 9. Accounts are installation-wide, and are not partitioned

`data/identity.db`, shared by every deployment and resolved from `ConfigPaths` by the server,
the admin CLI and `start.sh` alike.

This is the exception to decision 5, and it was learned the hard way: partitioning moved the
server's database while the admin CLI kept writing to the old path, so `admin add` reported
success and the login endpoint answered 401 to the same credentials. Both halves worked, against
different files.

The principle underneath: **history belongs to a deployment, identity belongs to the
installation.** One person signs in to the simulator and to the live system; requiring an
account per run mode is friction with no safety behind it, and the first response to it would
be one password reused everywhere.

## Consequences

Aetos can be run against a real sensor by someone with the hardware, in one command, with the
caveats stated by the system rather than only by a document: a start-up warning naming FM-050
and FM-052, an interface tooltip, and a health row that names a sensor that is not talking.

Run mode LIVE is now **built**. What is not built is any claim that the hardware behind it is
adequate — that is measurement, not code, and `sensors/adapters/uart_fmcw_radar/README.md`
holds the sequence that would establish it.

`sensor_health` is a new capability on `ApiServices` and a new obligation on whoever assembles
a pipeline. It is optional and defaults to reporting NOT_CONNECTED, so a caller that does not
supply it gets an honest answer rather than a missing row.

## References

- `docs/live_mode.md` — the operator's guide to running it
- ADR 0002, ADR 0017 — the contract this mode selects between implementations of
- `docs/failure_modes.md` — FM-010, FM-050, FM-051, FM-052
