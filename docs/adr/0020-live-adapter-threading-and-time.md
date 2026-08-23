# ADR 0020 — Live adapters read on their own thread, and a cycle is still one instant

**Status:** Accepted
**Date:** 2026-08-22

## Context

The first live sensor arrived and broke two assumptions that had held for the whole life of the
project, neither of which anybody had written down, because nothing could violate them while
every source was simulated.

**The simulated adapter is polled, and answers.** `get_detections()` computes what the sensor
would have seen at this instant and returns it. It costs microseconds, it never blocks, and it
returns at most one observation per object because it throttles itself to its own configured
update rate and is called once per pipeline cycle.

A serial sensor does none of that. It transmits continuously on its own schedule, a read blocks
until bytes arrive or a timeout expires, and whatever is buffered when the pipeline asks is
however many frames happened to accumulate since it last asked. Both differences turn out to
matter, and the second one is the dangerous one.

## Decision

### 1. A live adapter owns a reader thread; the pipeline drains it

The reader thread holds the port and the decoder. `get_detections()` takes what the thread has
produced and returns.

The alternative — reading inside `get_detections()` — was rejected because the pipeline steps
synchronously on the event loop. A blocking read there stalls tracking, classification, risk,
alerting and the WebSocket broadcast for every other sensor at the site, for as long as one
cable takes to answer.

The queue between the two is bounded, and when it fills the **oldest** frame is dropped and
counted (ADR 0008). Oldest because a live picture wants the freshest observation and a stale
detection would be gated out by the tracker anyway; counted because silent dropping is what
turns a saturated pipeline into a sensor that merely seems less sensitive.

### 2. Observation time is reconstructed from arrival time

A frame's last byte arriving is not the observation happening. Between them sit the device's own
processing and the frame's time on the wire, and the second is exactly computable:

```
observation  =  arrival  −  frame_bytes × 10 / baud  −  sensor_latency
```

Ten bits per byte, because the line is 8N1. The sensor's internal latency is declared rather
than measured — it cannot be seen from the host — and `timing.timestamp_uncertainty_ms` is how
wrong that declaration is allowed to be.

Worth the arithmetic because timing error is **systematic**. It does not average out over
updates, and at 20 m/s every 100 ms of it is two metres of position error in a consistent
direction — which the filter will happily absorb into a velocity estimate.

Every frame in one read is attributed to that read's completion, which is an approximation
bounded by the read timeout. `Ld2451DriverConfig.check_against` therefore refuses a read timeout
that is not below one update period, so the approximation can never span two observations.

### 3. Silence on the link is the adapter's business; an empty sky is not

`sensors/adapters/README.md` has always said an adapter must not decide whether its own silence
means a fault, because whether a quiet sensor is broken is site policy and belongs to
`backend/system_health` (FM-010). That still holds, and a live adapter draws a line inside it:

- **No bytes at all** is a link fault, and the adapter reports OFFLINE. This device transmits
  frames continuously whenever it is powered — an empty sky produces *empty frames*, which
  decode perfectly well — so total silence means the cable, the power or the port, all of which
  the adapter can legitimately know about.
- **Frames arriving with no targets in them** is an empty sky, and the adapter says nothing
  about it beyond passing on that it saw nothing.

The distinction is only available because the protocol has an idle message. A sensor that says
nothing when it sees nothing would leave the adapter unable to tell the two apart, and it would
have to report neither.

### 4. A tracking cycle is one instant, and batching must respect that

`TrackManager.process` predicts every track to now and runs a **single** association pass, which
assigns at most one detection per track. Its docstring said so — "every track is compared
against detections at the same instant" — and nothing had ever tested it, because nothing could
break it.

A live adapter breaks it immediately. A 10 Hz radar against a 0.5 s tick hands over five frames,
and the four after the first cannot be associated to the track the first one just updated, so
each initiates a new one. **One target in front of the radar produced nine tracks.**

So `Pipeline.step` now splits a cycle's detections into groups holding at most one observation
time per sensor, and calls `process` once per group in time order
(`backend/tracking/batching.py`). One target produces one track.

The rule is structural rather than numerical — no epsilon to tune. Detections from one frame
share a timestamp exactly and stay together; successive frames from one sensor land in
successive groups; and two *different* sensors stay in the same group even though their
timestamps differ by microseconds, which is what preserves cross-sensor association within a
cycle. A tolerance-based rule would have got that last case wrong, and the symptom would have
been fusion quietly never happening.

## Rationale

Point 4 is why this record exists. The other three are engineering; that one is an architectural
assumption that was true, undocumented, load-bearing, and false the moment real hardware
arrived — and its symptom was a swarm of phantom tracks, which on a counter-UAS picture is
indistinguishable from the thing the system exists to detect.

It is also the clearest evidence available for why the simulator, however carefully built, is
not a substitute for connecting something. The simulated adapter satisfied the assumption *by
construction*, so no scenario, no chaos test and no amount of provenance guarding could have
found this. Making the assumption explicit and tested is what stops it being rediscovered by the
next driver.

## Consequences

- Live adapters are inherently concurrent, which the simulated one is not. Their state is behind
  a lock, `stop()` joins the reader, and how long a stop takes is bounded by the read timeout —
  which is why that timeout is short as well as being bounded above by the update period.
- `Pipeline.step` runs one tracking pass per observation instant rather than one per cycle. A
  deployment whose sensors are all slower than its tick gets exactly one group and pays nothing,
  which is why this changed no existing behaviour.
- Classification's per-cycle evidence is merged across groups, with the freshest observation
  winning for a track seen more than once.
- A cycle with no detections still runs one tracking pass. That is what coasts tracks and ages
  them, and skipping it on a quiet cycle would leave dead tracks on screen for as long as the
  sky stayed empty.
- The pipeline tick is no longer the system's effective update rate. A faster sensor now
  contributes every observation it makes, which is what the tracker wanted all along.

## Alternatives considered

- **Have the adapter throttle to the pipeline's cadence.** Simplest possible fix for point 4:
  return only the most recent frame. Rejected — it discards real observations to suit the
  consumer's convenience, hides the sensor's true update rate from the conformance suite, and
  silently lowers the effective detection rate the whole system is tuned against.
- **Teach the associator to accept several detections per track in one pass.** The general
  solution, and a much larger change to the part of the system where identity swaps happen
  (ADR 0005). Rejected for now: the batching rule gets the same outcome without touching
  association, and this is not the moment to reopen the gate.
- **Group by a timestamp tolerance.** Rejected under §4 — it needs a number that must stay
  smaller than every sensor's update period and larger than every cross-sensor skew, and the
  failure when it drifts is silent.
- **Run the adapter's reads in an executor rather than a thread it owns.** Rejected: the adapter
  would then need the event loop, which would make it unusable from a synchronous test or from
  the bench tool, and the conformance suite would have to become asynchronous to exercise it.
