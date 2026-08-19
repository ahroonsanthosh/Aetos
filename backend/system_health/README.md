# `backend/system_health` — not built

Empty on purpose, like `backend/fusion`. The one file is `__init__.py`.

## What would live here

The watchdog: deciding, from what sensors report about themselves, whether the system is healthy,
degraded, or lying to its operator. Rate analysis over a window, a degraded-mode controller, and
the self-test.

## What exists instead, and why it is not this

`GET /system/health` reports a `sensors` row built from each adapter's own `health()`, polled per
request (ADR 0018). It is honest and it is useful — it will tell you a port could not be opened
or that a byte stream is not framing — but it is **not** a watchdog:

- No rate analysis. It cannot tell a sensor that has gone quiet from a sky that is empty, which
  is the whole of FM-010, because that judgement needs a window and a site-level policy.
- No degraded-mode controller. Nothing changes behaviour when the picture thins.
- No self-test. Nothing exercises the pipeline to prove it still works when nothing is flying.

## Why the distinction is kept sharp

Adapters report facts about themselves and make no claim about what silence means; the health
endpoint makes a blunt, pessimistic judgement about those facts. A real watchdog would make a
*policy* judgement — thresholds that differ between a 10 Hz imaging sensor and a 1 Hz
radio-frequency one — and that policy belongs here, not scattered across adapters.

Calling the current row a watchdog would be the failure this system is written against: a green
light nothing computes.
