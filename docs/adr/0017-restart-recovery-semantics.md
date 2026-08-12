# ADR 0017 — What survives a restart, and what is closed out

**Status:** Accepted
**Date:** 2026-08-12
**Relates to:** [ADR 0006](0006-event-store-as-source-of-truth.md) (the log is the source of
truth), FM-030, FM-032

## Context

ADR 0006 says in-memory state is a projection of the event log and is rebuilt by replay. That
is true of the projection and of alerting, both of which rebuild at startup. It was never true
of the tracker, and the gap did not present as missing state — it presented as the opposite.

A restart left every live track sitting in the projection at the position and confidence it
held the instant the process died. `GET /tracks` reads the projection; the projection is only
ever written by a tracker; the tracker that owned those tracks no longer existed. Nothing in
the new process was capable of moving them out of CONFIRMED, so they stayed on the operator's
display permanently, over sky nothing was watching.

Underneath that, the track identifier generator restarted at `TRK-00001` while `track_history`
— keyed `(track_id, seq)`, never pruned, retained forever by spec §5 — still held the previous
run's rows. The new run's history interleaved with the old under one identifier. A lost track
is recoverable by waiting for re-acquisition; this is not, because the evidence that the two
were different objects is gone.

## Decision

### 1. Tracks do not survive a restart. They are closed out, with the reason recorded.

The tempting alternative is to rebuild them. Every lifecycle event carries a full `Track`
snapshot (`TRACK_SNAPSHOT_KEY`), so the *published* state genuinely could be restored.

What cannot be restored is the filter covariance behind it. Covariance is the estimate's
uncertainty and the thing the association gate is derived from, and it is deliberately not
published — `_ActiveTrack` holds it precisely so consumers cannot depend on the estimator's
internal representation. Rebuilding a track around an invented covariance would produce a track
that looks exactly as established as the one that died while being an estimate nothing
supports. That is the same unearned confidence this record exists to remove, arrived at from
the other direction.

So Aetos re-acquires. The sky is still there; a real object is re-detected within a cycle or
two, under a new identifier. `TrackManager.resume_after_restart` emits `TRACK_LOST` for every
track the previous process left live, carrying `reason: backend_restarted`.

The reason matters as much as the retirement. "The system lost the object" and "the object went
away" are opposite conclusions for anyone reconstructing an incident afterwards, and a bare
`TRACK_LOST` cannot distinguish them.

### 2. Identifiers resume above every identifier ever issued, not above the live ones

Including tracks long since LOST, because a LOST track keeps its identifier in `track_history`
forever. The resume point comes from the store rather than from a counter.

The same rule, stated generally: **a sequential identifier resumes from the high-water mark, not
from a census.** Counting rows is only equivalent while nothing has ever failed. The event store
had exactly this bug — `append` draws its identifier before the INSERT, so a failed write burns
one permanently, after which the row count is short of the high-water mark and the next append
after a restart re-issues an identifier the log already holds. `event_id` is UNIQUE, so the
store refused it: one transient disk error became a permanent inability to append, surviving
the restart meant to clear it. Gaps in the sequence are the correct outcome and are preserved.

### 3. Alerts are restored first, then their tracks are retired

Ordering, not accident. `alerting.rebuild()` runs before track recovery, so the first cycle
clears each restored alert with `track_lost` rather than leaving it pointing at a track nothing
maintains.

The consequence is that an alert acknowledged before the restart re-raises unacknowledged if
the object is still there. That is intended. An acknowledgement attests that a human saw a
particular track; after a discontinuity in which the system lost every track, asking them to
look again is the safe direction to be wrong in.

### 4. A pipeline that has stopped must not read as healthy

When the pipeline stops, nothing else does. The API answers, the WebSocket stays open, the
projection serves what it has — none of them depend on a cycle having run. All of them go on
presenting the last good picture with nothing saying it is frozen.

`backend/system_health/cycles.py` records how recent cycles went and reports it as a `pipeline`
health component: degraded at one consecutive failure (a cycle that raised produced no picture,
and the operator is looking at a stale one from that moment, not from the third occurrence),
unavailable at five (nothing is clearing it), self-clearing on recovery (a banner that stays up
after the fault clears is a banner operators learn to ignore).

This is not the watchdog. Sensor-silence detection and the degraded-mode controller (FM-010,
spec §16) remain unbuilt, and the health listing names that gap `sensor_watchdog` rather than
implying the whole module is missing.

## Consequences

**Good.** FM-030's worst form — a confident track over dead sky, unremovable — is closed, and
the audit trail can no longer be corrupted by a restart. FM-032's silent half is closed: an
event-store outage is now visible on the surface operators and monitoring probes actually read.

**Costs.** Track continuity genuinely does not survive a restart, and this record makes that a
stated property rather than an unexamined one. An operator watching a contact through a restart
sees it re-acquire under a new identifier, and any acknowledgement they had given is withdrawn.
Both are deliberate, and both are worse experiences than a correct rebuild would be — the
argument is that a correct rebuild is not available, and the alternative on offer was a
confident lie.

**Not addressed.** Classification evidence memory and risk loiter history are still lost on
restart (a track re-acquires as `unknown` and rebuilds confidence over subsequent cycles). That
is a degradation rather than a falsehood — the system knows less and says so — so it is left
for the fusion work rather than fixed here.
