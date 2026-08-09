# `backend/common` — cross-cutting primitives

## Responsibility

Primitives that every other backend module needs and that belong to none of them:

| Module | Provides |
|---|---|
| `clock.py` | `Clock` interface plus `SystemClock`, `SimClock`, `OffsetClock` |
| `geodesy.py` | WGS-84 ↔ local ENU conversion, `LocalFrame`, distance and bearing helpers |
| `identifiers.py` | Deterministic sequential ID generation for detections, tracks and events |

## Interface boundary

**`common` imports nothing from any other backend module.** Not `tracking`, not `events`, not
`risk`, not `api`. This is an enforced rule rather than a guideline — the moment `common`
depends on a domain module it becomes a dumping ground and starts producing import cycles.
`tests/unit/test_module_boundaries.py` asserts the dependency direction.

The corollary is that anything with domain logic does not belong here. A helper that knows
what a track is belongs in `tracking`. A helper that knows what a protected zone is belongs
in `zones`. `common` is deliberately small and is kept so.

## Why these three exist

- **Clock** — nothing may call `datetime.now()` directly, because replay, deterministic tests
  and simulated clock skew all become impossible once wall-clock calls are scattered through
  the codebase. See [ADR 0003](../../docs/adr/0003-injectable-clock.md).
- **Geodesy** — the schema and API speak WGS-84 degrees; all maths between them happens in
  local ENU metres. This module is the conversion at both boundaries. See
  [ADR 0004](../../docs/adr/0004-enu-tracking-frame.md).
- **Identifiers** — deterministic and sequential rather than random, so a replayed scenario
  produces identical IDs and can be compared against the original run.

This module exists as a documented deviation from the specified repository structure. See
[ADR 0009](../../docs/adr/0009-repository-structure-deviations.md).
