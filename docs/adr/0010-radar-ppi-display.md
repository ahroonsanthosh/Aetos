# ADR 0010 — The operational map is a radar-style PPI, not a geographic basemap

- **Status:** Accepted
- **Date:** 2026-08-09
- **Supersedes:** the frontend mapping decision in [ADR 0001](0001-language-and-stack.md).
  The backend half of ADR 0001 (Python, FastAPI, Pydantic, SQLite) stands unchanged.

## Context

ADR 0001 chose MapLibre GL for the operator map, reasoning about open-source licensing and
access tokens. That was a decision about *which geographic map library* to use, taken before
the operator interface had been specified.

The Aetos frontend specification describes something different: a tactical display with range
rings, a bearing grid, cardinal marks, sensor positions and coverage arcs, centred on the
protected site. That is a plan position indicator, not a slippy map. The two answer different
questions — a basemap answers "what is on the ground there", a PPI answers "how far away and
in what direction, relative to what we are defending".

## Decision

The operational map is a purpose-built **PPI display**, rendered as SVG, centred on the site
reference point. No geographic map library, no basemap tiles.

Track rendering is separated from the display substrate, so a geographic view can be added
later as an alternate mode without rewriting how tracks are drawn.

## Rationale

- **It is what the interface is for.** Counter-UAS operation is inherently site-centric:
  everything that matters is a range and a bearing from the asset being protected. The
  backend already thinks this way — it works in a local ENU frame anchored at the site
  reference (ADR 0004) — so a site-centred display is the natural presentation of state the
  system already holds, rather than a reprojection of it.
- **No network dependency.** Basemap tiles come from a tile server. Without one the map is
  blank, which makes an offline demonstration impossible and an air-gapped deployment
  awkward. A PPI has no external dependency at all and renders identically offline. For a
  system whose replay is explicitly the primary demonstration mechanism (spec §18), a display
  that can fail to load is a poor foundation.
- **Range rings are native to it and awkward on a basemap.** Concentric distance rings, a
  bearing grid and sensor coverage arcs are the PPI's own coordinate system. On a Mercator
  basemap they are overlays that need reprojection and that distort with latitude.
- **It removes a dependency rather than adding one.** SVG rendering of a few hundred elements
  needs no library.

## Consequences

- No geographic context: terrain, roads and buildings are not shown. For a site-centric air
  picture this is an acceptable loss, and it is recorded as a known limitation. If situating
  a site in real terrain becomes a requirement, the separation between track rendering and
  display substrate is what makes a second view addable.
- Range and bearing are computed for display from the geodetic position the API returns, in
  the local ENU frame. These are **site-relative**, not sensor-relative, and the interface
  labels them as such — a bearing an operator might read as "what the radar saw" would be a
  misrepresentation.
- Performance is a rendering concern rather than a tile-loading one. At the V0 ceiling of
  twenty simultaneous tracks with history trails, SVG is comfortable.

## Alternatives considered

- **Keep MapLibre GL.** Real geographic context, and the decision already logged. Rejected:
  it answers a question the operator is not asking, needs network tiles to show anything, and
  makes the spec's core display elements into distorting overlays.
- **Both from the start.** Rejected as premature. The abstraction is in place; building the
  second view before anyone has asked for it is speculative work.
