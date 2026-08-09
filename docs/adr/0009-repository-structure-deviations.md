# ADR 0009 — Two deviations from the specified repository structure

- **Status:** Accepted
- **Date:** 2026-08-08

## Context

Spec §19 fixes the repository layout and requires that any deviation be flagged and
justified rather than made silently. Two are needed.

## Decision

### 1. No nested `cuas-os/` directory

The specification roots the tree at `cuas-os/`. The repository is named `Aetos`. The tree is
laid out directly at the repository root (`docs/`, `backend/`, `sensors/`, `scenarios/`,
`frontend/`, `config/`, `tests/`) rather than inside a redundant `Aetos/cuas-os/` nesting.

Nothing about the structure changes; only the absent extra level.

### 2. `backend/common/` added for cross-cutting primitives

The specified module list has no home for primitives every module depends on:

- `clock.py` — the injectable clock (ADR 0003)
- `geodesy.py` — WGS-84 ↔ ENU conversion (ADR 0004)
- `identifiers.py` — deterministic ID generation for detections, tracks and events
- `config.py` — loading and validating configuration from `config/`

Placing these in any existing module would be wrong. The clock is not a tracking concern;
geodesy is used by tracking, zones, risk and the simulator alike. Duplicating them is worse.

`backend/common/` therefore holds only primitives with **no dependency on any other backend
module**. This is an enforced rule, not a guideline: `common` importing from `tracking`,
`risk`, `events` or any sibling would make it a dumping ground and create import cycles. A
unit test asserts the dependency direction.

## Consequences

- `backend/common/` is deliberately small and will be kept so. Anything with domain logic
  belongs in a domain module.
- The dependency-direction test is the mechanism that keeps this true over time.
