# `backend/system_health` — the watchdog

Decides, from what the sensors report about themselves, whether the system is healthy, degraded,
or lying to its operator. Closes FM-010.

## The distinction this module exists to hold

**Loss of contact with a sensor is a fact. Absence of detections is not.**

A driver that cannot open its port is definitely broken. A radar that has reported nothing for
ninety seconds may be broken, or may be pointed at an empty sky, and no amount of looking at the
sensor will tell you which. So the verdict vocabulary keeps the two apart:

| Verdict | Claim |
| --- | --- |
| `OK` | In contact and producing detections within the interval this site expects. |
| `QUIET` | In contact, healthy by its own account, silent past the policy window. **Not a fault claim.** |
| `DEGRADED` | Running with reduced capability the adapter itself named, or failing its polls. |
| `OFFLINE` | Not observing at all. Contact is lost. A definite fault. |
| `NOT_STARTED` | Attached, never started. Not a fault; nothing has been asked of it. |

Collapsing `QUIET` into a fault would cry wolf on every calm night, and an operator who learns
to ignore the row has disabled the mechanism. Collapsing it into `OK` would be the green light
this system is written against. Both mistakes end the same way, which is why the words stay
separate all the way to the display.

A `QUIET` verdict carries the number of seconds and the threshold it was compared against, so it
can be argued with without opening the policy file.

### The verdict must settle, not flap

Silence is a sawtooth. A source producing one detection every 35 seconds against a 30-second
window crosses the threshold, gets a detection, drops under it, and crosses again — forever.
Compared naively that is a verdict flipping twice a minute, a blinking display, and some two
hundred status events an hour per sensor burying the one transition that mattered.

So a `QUIET` verdict is withdrawn only after the source sustains healthy reporting for
`recovery_dwell_windows` of its own silence window. The bound is **greater than one window**,
enforced on the field rather than left to the deployment: a flapping source accumulates exactly
one window of apparent health between crossings, so any dwell of one or less is satisfied by the
flap itself. A genuinely marginal sensor therefore settles on `QUIET` and stays there, which is
the true description of it.

The dwell guards the verdict this module *infers*. It is never applied to withdrawing `OFFLINE`
or `DEGRADED`: those are the adapter's own claims, and a source that has withdrawn its claim is
back. Holding `BLIND` over a map that is visibly drawing tracks would be a lie in the other
direction, and a driver whose state flaps is a defect belonging in the driver.

## Posture

Derived across all sources, and reported as a banner rather than a row because it is the one
line an operator reads before deciding whether to trust anything below it.

- `NOMINAL` — every source in contact. Includes the case where all of them are quiet: a calm
  sky is not a fault.
- `DEGRADED` — at least one source faulted, at least one still observing. Not an average; four
  radars and one dead cable is degraded, because the question is never "are most of mine fine".
- `BLIND` — nothing is observing, or nothing is attached. The display is memory, and an empty
  one means nothing at all.
- `STALLED` — see below. Note that an assessment over **no** sources never ages into this: a
  staleness claim says observations *stopped*, which needs something to have been observing. An
  idle deployment stays `BLIND`, because raising a fault on an ordinary start is how a watchdog
  gets muted.

Transitions are appended to the log as `SENSOR_STATUS_CHANGED`, `DEGRADED_MODE_ENTERED` and
`DEGRADED_MODE_EXITED`, on transition only. A log that recorded a healthy radar twice a second
would bury the moment it stopped. Both directions of the posture change are recorded: a recovery
nobody logged is indistinguishable from one that never happened.

## The self-test

Every component in this system reports on itself, and a component that has stopped running
reports nothing — which renders identically to one with nothing to say. The watchdog timestamps
each pass; `report()` ages that timestamp against the clock at the moment somebody asks. A
pipeline that has stopped stepping therefore surfaces as `STALLED` to the API even though
nothing inside the pipeline could possibly say so.

It works precisely because it is evaluated *outside* the thing it is testing. `report()` appends
nothing and mutates nothing, which is what makes it safe on the request path — and it is also
why nothing reachable from the API may cause an assessment: a request that could trigger one
would destroy the endpoint's ability to tell a system that is stepping from one that is only
answering.

The wiring assesses once during construction, so the first request is answered from a real
judgement rather than from never having run — a deployment with no adapters is never stepped at
all, and that one pass is the only one it would otherwise get.

`stall_threshold_s` must exceed the pipeline tick, or the system would declare itself stalled on
every healthy cycle. `SystemHealthPolicy.validate_against_tick` is checked at startup, where
both numbers are known; the policy file cannot see the tick and so cannot check it alone.

## What this module deliberately does not do

**Change behaviour.** Degraded mode here is a *declaration*: recorded in the log, shown on the
display, and nothing else. No threshold moves, no track is scored differently. Silently altering
the assessment pipeline when the picture thins would mean an operator comparing two incidents
was comparing two different systems.

## Layering

Adapters report facts about themselves and make no claim about what silence means. `Pipeline`
gathers those reports and overrides an adapter that raised on its last poll — a driver that
throws and then claims OK is describing an intention. The watchdog judges what arrives, against
a window and a site policy neither of the other two has. `GET /system/health` reports the
judgement without restating it: the posture appears once, on the response, never duplicated into
the component row, because two representations of one fact are free to disagree.

## Layout

| File | |
| --- | --- |
| `models.py` | Verdicts, posture, and the assessment records. The vocabulary and its limits. |
| `policy.py` | Thresholds, loaded from `config/system_health/policy.json`. |
| `watchdog.py` | The watchdog itself. |
