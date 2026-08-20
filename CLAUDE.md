# Aetos — working notes for Claude

## Read these first

- `README.md` — what the system is and how to run it.
- `docs/architecture.md` — how it is built. Living document.
- `docs/vision.md` — **the long-term product vision.** Aspirational, not a claim about
  current behaviour. Read it before designing anything new, so today's decisions do not
  foreclose it.
- `docs/adr/` — decision records. Numbered, append-only in spirit.

## The two rules that override convenience

**1. The scope boundary.** Aetos is software only and ends at a recommendation presented to a
human. No effector control, no weapons integration, no autonomous response execution. The most
severe output is the recommendation category `RESPONSE_RECOMMENDED`, which is advice. This is
architectural, not a phase-one simplification. `response/` stays empty until the design pass
in `response/README.md` has been done. Guard tests in `tests/unit/test_module_boundaries.py`
fail the build if activation vocabulary appears in `backend/` or `sensors/`.

`docs/vision.md` describes future response capabilities (§8–§11). They are decision support —
knowing resource state and articulating options — and they do not relax the boundary. Keep
`RECOMMENDED`, `AUTHORISED` and `EXECUTED` as three distinct states with three distinct
authors.

**2. The governing principle.** The tracking engine cannot tell whether a detection came from
the simulator or from a physical sensor. One canonical `Detection`, one `SensorAdapter`
interface, and the ADR 0002 guard test that runs a scenario with simulator-privileged fields
stripped and requires identical track output.

## Conventions

- Module boundaries are explicit interfaces and are never collapsed into a combined pipeline
  function.
- The event log (`backend/events/`) is the source of truth (ADR 0006). Downstream reads by
  sequence number.
- Never claim a capability the code does not have. Documentation that describes unbuilt
  behaviour is a defect — see ADR 0011 and the stale-claims fix in `frontend/`.
- Record significant design decisions as a new numbered ADR under `docs/adr/`.
- British spelling in prose (normalise, authorise, behaviour).

## Running and testing

```bash
./start.sh              # sets up, builds, serves API + UI on :8000
./start.sh --list       # the nine scenarios
pytest                  # unit, integration, scenario replay, chaos
```
