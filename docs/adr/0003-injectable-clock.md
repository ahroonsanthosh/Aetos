# ADR 0003 — All time comes from an injected clock

- **Status:** Accepted
- **Date:** 2026-08-08

## Context

Spec §18 requires that every scenario run be replayable with pause, fast-forward and
slow-motion. Spec §7 requires scripted scenarios that advance on their own timeline. Chaos
testing (§17) requires simulating clock skew between sensors.

None of that is possible if modules call `datetime.now()` directly. Replay is the feature
most often designed in and then found to be unimplementable, because wall-clock calls are
scattered through the codebase by the time anyone tries.

## Decision

No module in `backend/` or `sensors/` calls `datetime.now()`, `time.time()` or
`time.monotonic()` directly. All time is obtained from a `Clock` passed in at construction.

Three implementations:

- `SystemClock` — wraps wall time. Used in live operation.
- `SimClock` — manually advanced. Used by scenarios, replay and the majority of tests.
  Supports a rate multiplier (pause = 0, slow motion < 1, fast-forward > 1).
- `OffsetClock` — wraps another clock with a fixed offset. Used to simulate per-sensor clock
  skew in chaos tests.

`Clock` exposes both `now() -> datetime` (UTC, for timestamps in the canonical schema) and
`monotonic() -> float` (for interval and timeout logic, which must not move backwards).

A lint-level test (`tests/unit/test_module_boundaries.py`) greps the source tree and fails
if a forbidden direct time call appears outside `backend/common/clock.py`.

## Rationale

Making the clock injectable from the first commit costs almost nothing. Retrofitting it
after twelve modules exist costs a rewrite of all twelve. The greping test exists for the
same reason as the guard test in ADR 0002: the convention will not hold on its own.

Separating `now()` from `monotonic()` matters because timeout logic that uses wall time
breaks under NTP correction and under replay fast-forward, in different and confusing ways.

## Consequences

- Every module constructor takes a clock. This is slightly more ceremony at every call site.
- Tests become deterministic and fast: track staleness after 30 seconds is tested by
  advancing a `SimClock` 30 seconds, not by sleeping.
- Replay is close to free — the same code runs, driven by a `SimClock` fed from the event
  log's recorded timestamps.
