# Chaos tests

Resilience tests for the failure modes in `docs/failure_modes.md`. A chaos test drives the
system into an adverse state and asserts on **what an operator would see afterwards** — which
is a different question from the one the unit suite asks, and it needs answering separately.

## What makes a test belong here

The unit suite asks whether a component computes the right answer. These ask what happens when
the answer cannot be computed: the sensor stopped, the disk filled, the process died, more
objects arrived than the system can carry.

The property nearly every test here is really protecting is one sentence:

> **The system is allowed to know less. It is not allowed to keep claiming the same confidence
> while knowing less.**

Losing a track is survivable and expected. A track that stays on the display at the position it
held when the data stopped is not, because the operator cannot tell it apart from a live one.
Almost every assertion in this directory is a variation on that distinction.

## Why several of these cannot be unit tests

Three of the defects these tests were written to catch are invisible to any single-process test,
because they only exist across a boundary the unit suite never crosses:

- **A restart** (`test_restart_recovery.py`) — every symptom appears between two processes.
  These tests build a real `Pipeline` on a real directory, throw it away, and build a second one
  over the same data. That is the restart, not a simulation of it.
- **A burned identifier** (`test_event_store_unavailable.py`) — a failed write consumes an id
  without storing a row. Nothing goes wrong until the *next start*, when the id sequence is
  restored and hands out an id that is already in the log.
- **A frozen pipeline** (same file) — the failure is not that a cycle raised. It is that
  everything else kept working, so the console went on looking authoritative while showing a
  picture that had stopped advancing.

## Injecting failure

Prefer the seam that produces the real shape of the outage rather than the most convenient one.

`PRAGMA query_only = ON` is used for the event-store tests because it fails writes while leaving
reads working — which is what a read-only filesystem, a full disk and a revoked grant all look
like. Closing the connection would have been easier and would have broken reads too, and a test
that broke reads would never have caught a health check that only reads. That health check was
the bug.

Avoid `chmod` on the database file: WAL keeps writing until the page cache flushes, so the
failure lands non-deterministically.

## Mutation testing

Every fix in this directory was verified by breaking it deliberately and confirming the suite
went red — the repository convention for anything with hysteresis, gating or recovery logic.
For the restart work that meant four separate mutations (recovery disabled entirely, ids not
resumed, retirement not emitted, wrong reason recorded), each of which is caught by a different
test. A green run against an unmutated build proves much less than most people assume.

## Coverage

| Failure mode | File | State |
|---|---|---|
| FM-010 sensor goes silent | `test_sensor_input_failures.py` | Lifecycle demotion and coasting honesty covered. **The watchdog itself is not built** — nothing detects that a *sensor* has stopped, as opposed to an object having left. |
| FM-011 malformed detections | `test_sensor_input_failures.py` | Covered, including that one bad sensor does not take its batch down with it. |
| FM-012 clock skew | `test_sensor_input_failures.py` | Flat bound, both directions, recorded distinctly. **Per-sensor skew estimation is not built.** |
| FM-013 duplicate detections | `test_sensor_input_failures.py` | Covered, including retransmission storms. |
| FM-014 saturation | `test_saturation.py` | Bound, shed events, and the degradation ordering are covered. **Quality-ordered shedding and degraded mode are not built** (ADR 0008 §2). |
| FM-030 backend restart | `test_restart_recovery.py` | Covered. Two defects found and fixed; see ADR 0017. |
| FM-032 event store unavailable | `test_event_store_unavailable.py` | Covered. Two defects found and fixed; see ADR 0017 §2 and §4. |
| FM-031 UI crash silencing alerts | — | Not written. Alerting is backend-side, so the structural claim holds, but nothing tests it. |

The "not built" notes are load-bearing. A test file named after a failure mode invites the
reader to assume the mode is handled, and the gaps above are exactly the ones that assumption
would hide.
