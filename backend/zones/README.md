# `backend/zones` — site geometry, geofencing and zone-crossing prediction

## Responsibility

Where something is relative to the geography a deployment defends, and when its current course
would put it somewhere else.

| Module | Provides |
|---|---|
| `site.py` | The `Site` model: reference point, zones, sensor placements |
| `geometry.py` | `SiteGeometry` — containment, signed boundary distance, time-to-boundary — and the site loader |

## Interface boundary

**Answers *where*. Never *how dangerous*.** A zone occupancy and a time-to-boundary are facts
about geometry; turning them into a score with reason codes is `backend/risk`, and turning that
into a recommendation is `backend/decision`. This module has no notion of a threat, and
`ZoneKind.severity` exists so risk can order zones without this module having to rank anything
itself.

**Depends on:** `backend/common/geodesy` for the frame and `backend/common/config` for the
load-and-validate machinery. Nothing else.

**Provides to risk and to the API:** `zones_containing`, `most_consequential_zone`,
`signed_distance_m`, `distance_to_protected_area_m`, `time_to_zone_s`,
`time_to_protected_area_s`.

The site loader lives here rather than in `backend/common/config.py` because `common` may not
import `backend.zones.site` (ADR 0009). `common` supplies generic machinery parameterised by
model type; this supplies the model and the naming rule.

## Everything happens in ENU metres

Configured geometry is WGS-84 because that is the interchange format. A `SiteGeometry` projects
it into the site's local tangent plane once at construction and answers every subsequent query
in metres (ADR 0004). Degrees never reach the maths: a degree of longitude is not a fixed
distance, so a containment test computed on latitude and longitude is wrong by a factor that
changes with latitude and differs between the two axes.

Two consequences worth stating plainly:

- **Altitude bounds are translated on projection.** A zone's `floor_m` and `ceiling_m` are
  metres above the WGS-84 ellipsoid; ENU `up_m` is metres above the *site reference altitude*.
  `ProjectedZone` stores `floor_up_m` / `ceiling_up_m` in the frame's own terms. A site whose
  reference sits 120 m above the ellipsoid would otherwise have every configured ceiling wrong
  by 120 m — FM-004 in miniature.
- **The tangent plane curves away from the ellipsoid with range.** At the far edge of an
  8 km monitored zone, constant ellipsoidal height reads a few metres low in `up_m`. That is
  well inside sensor uncertainty and inside FM-002's accepted bound, but it is a real effect and
  not a rounding artefact.

## Boundary contact resolves inwards

A point exactly on a zone edge, or exactly on a vertex, is **inside** the zone.

The rule has to be chosen and written down, because otherwise it is decided by whichever way a
floating-point comparison happens to fall, and the two resolutions produce different alerts for
identical input. Inwards is the fail-safe direction: a zone exists to be noticed, and something
grazing the edge of a protected area should register rather than slip through the tie.

Mechanically, containment is `signed_distance_m(point) <= BOUNDARY_TOLERANCE_M`, which makes the
circle and the polygon behave identically and avoids depending on a ray-crossing count at
exactly the points where a crossing count is ill-defined. The polygon's crossing-number test is
still used, but only to choose the *sign* of a distance that is well defined either way.

## Time-to-boundary

`time_to_zone_s(zone_id, point, velocity)` gives the seconds until a constant-velocity course
first satisfies the zone's footprint **and** its altitude bounds at the same instant, or `None`
if it never does. Risk assessment takes it as an input (spec §10).

Distance to the boundary divided by speed is wrong in every case that matters, so each is
handled explicitly:

| Case | Answer | Why not the obvious thing |
|---|---|---|
| Already inside | `0.0` | The question is when it reaches the zone; something already there reaches it now. Not `None`, and not the time to the far side |
| Moving away | `None` | The algebra still has a root — in the past. Negative roots are discarded, not taken in absolute value |
| Tangential | `None` | A grazing course gives a discriminant of exactly zero and one touching instant. Reporting an entry would hand risk an imminent crossing for an object that never crosses |
| Stationary | `0.0` inside, `None` outside | No amount of time moves it |
| Below a zone floor and level | `None` | Footprint and altitude must hold together. Crossing over a corridor while still below it is not entering it |

Non-convex polygons are handled by splitting the course at every edge crossing and probing each
resulting interval, because containment can only change at a crossing. A single entry/exit pair
would be wrong for any footprint a course can leave and re-enter.

The module is deliberately clock-free (ADR 0003): it returns a duration, not an instant. The
caller holds the track's timestamp and the injected clock, and converting "in 42 seconds" into a
time is its business.

## Configuration, never code

No site, coordinate, radius or threshold appears in this module. Sites are JSON documents in
`config/sites/`, validated on load, and the shipped one is fictional test data. The two numeric
constants here — `BOUNDARY_TOLERANCE_M` and the stationary-speed guard — are floating-point
tie-breaks well below any physically meaningful quantity, not policy settings.
