# ADR 0001 — Language and stack

- **Status:** Accepted
- **Date:** 2026-08-08

## Context

Aetos needs a backend that performs state estimation (Kalman filtering), combinatorial
assignment (track-to-detection association), and geodetic maths, while also serving a REST
API and a real-time push stream. The classification module must be swappable for a learned
classifier later. The frontend is an operator map with live tracks.

## Decision

**Backend:** Python 3.11, FastAPI (REST and WebSocket in one application), Pydantic v2 for
schema definition and validation, NumPy and SciPy for filtering and assignment, SQLite in
WAL mode behind a repository interface.

**Frontend:** React with Vite, MapLibre GL for mapping.

## Rationale

- The tracking, fusion and risk maths is Python-native. `scipy.optimize.linear_sum_assignment`
  gives us Hungarian assignment for free; writing it ourselves in another language is
  avoidable risk in the exact place identity swaps happen (see ADR 0005).
- Pydantic v2 is not a convenience here, it is a requirement. The canonical Detection schema
  is versioned and must reject malformed sensor input as a *tested behaviour*
  (spec §17, chaos tests). Pydantic makes that declarative.
- A future learned classifier is a Python story. Keeping the classification module in the
  same runtime means swapping it is an interface change, not a cross-process redesign.
- MapLibre GL is open source and does not require a proprietary access token, so no
  vendor account is a precondition for running the system. It degrades to a blank
  coordinate canvas without network tiles, which keeps offline demos working.
- SQLite in WAL mode gives single-writer durability with concurrent readers, which is
  exactly the event-store shape we need (ADR 0006), at the scale we need (tens of tracks,
  tens of detections per second). It is placed behind a repository interface so PostgreSQL
  is a swap rather than a rewrite.

## Consequences

- Two languages in the repository. Accepted: the frontend/backend contract is the REST and
  WebSocket API, which is versioned and documented, not shared in-process types.
- Python's GIL bounds single-process throughput. At the V0 config ceiling (20 simultaneous
  tracks) this is not close to binding. If it becomes binding, the event-log architecture
  already permits splitting ingestion from tracking across processes.

## Alternatives considered

- **TypeScript end to end.** Shared types across the stack, one language. Rejected: we would
  hand-write the Kalman filter and Hungarian assignment, and the future ML classifier
  becomes a cross-process integration problem rather than a module swap.
- **Vanilla JS frontend.** Faster to stand up. Rejected: the C2 UI grows track detail
  panels, alert lists and replay transport controls; component state management is wanted
  early rather than retrofitted.
