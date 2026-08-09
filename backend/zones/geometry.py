"""Geofencing and zone-crossing prediction.

Every calculation in this module happens in the site's local ENU tangent plane, in metres
(ADR 0004). Degrees never reach the maths: a degree of longitude is not a fixed distance, so
a containment test or a boundary distance computed on latitude and longitude is wrong by a
factor that changes with latitude and differs between the two axes.

Configured zone geometry is WGS-84, so a `SiteGeometry` projects it once at construction and
answers every subsequent question in the flat frame. Projection is not repeated per query.

What this module answers: where something is relative to the site's geography, and when its
current course would put it somewhere else. What it does not answer: how dangerous that is.
Weighting a zone occupancy into a score belongs to `backend/risk`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from backend.common.config import (
    ConfigError,
    ConfigPaths,
    EnvironmentConfig,
    load_config,
    site_config_path,
)
from backend.common.geodesy import EnuPoint, GeodeticPoint, LocalFrame
from backend.zones.site import CircularGeometry, GeoPoint, Site, Zone, ZoneKind

BOUNDARY_TOLERANCE_M = 1e-9
"""Floating-point tie-break, not a policy setting.

A point whose distance to a boundary is within this of zero counts as inside. See
`ProjectedZone.contains_horizontally` for why boundary contact resolves inwards.
"""

_STATIONARY_SPEED_MPS = 1e-9
"""Below this the object is treated as not moving. Extrapolating a picometre per second to a
boundary crossing produces an arithmetically valid answer of geological magnitude, which is
not an answer."""


@dataclass(frozen=True, slots=True)
class EnuVelocity:
    """Velocity in the site's local frame, metres per second.

    Lives here rather than in `backend/common/geodesy` because velocity is not part of the
    coordinate conversion contract; it is what this module needs to extrapolate a course.
    """

    east_mps: float
    north_mps: float
    up_mps: float = 0.0

    @property
    def horizontal_speed_mps(self) -> float:
        return math.hypot(self.east_mps, self.north_mps)


@dataclass(frozen=True, slots=True)
class Circle:
    """A circular zone footprint in ENU metres."""

    centre_east_m: float
    centre_north_m: float
    radius_m: float

    def signed_distance_m(self, east_m: float, north_m: float) -> float:
        return (
            math.hypot(east_m - self.centre_east_m, north_m - self.centre_north_m)
            - self.radius_m
        )


@dataclass(frozen=True, slots=True)
class Polygon:
    """A polygon zone footprint in ENU metres. The ring is closed implicitly."""

    vertices: tuple[tuple[float, float], ...]

    def signed_distance_m(self, east_m: float, north_m: float) -> float:
        nearest = min(
            _distance_to_segment_m(east_m, north_m, a, b) for a, b in self.edges()
        )
        return -nearest if self.encloses(east_m, north_m) else nearest

    def encloses(self, east_m: float, north_m: float) -> bool:
        """Strict crossing-number test, used for the sign of the distance.

        The half-open comparison `(ay > north) != (by > north)` is what makes a horizontal ray
        through a vertex count once rather than twice or zero times. Points lying exactly on
        the boundary are not decided here — the caller decides those from the distance, which
        is well defined where the crossing count is not.
        """
        inside = False
        for (ax, ay), (bx, by) in self.edges():
            if (ay > north_m) != (by > north_m):
                crossing_east = ax + (north_m - ay) / (by - ay) * (bx - ax)
                if east_m < crossing_east:
                    inside = not inside
        return inside

    def edges(self) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        return list(zip(self.vertices, self.vertices[1:] + self.vertices[:1], strict=True))


Shape = Circle | Polygon


@dataclass(frozen=True, slots=True)
class ProjectedZone:
    """A configured zone with its geometry expressed in the site's local frame.

    Altitude bounds are stored relative to the frame's origin rather than as the ellipsoidal
    heights the site document declares, because ENU `up_m` is measured from the site reference
    altitude. Mixing the two is the FM-004 error in miniature: a site reference 200 m above
    the ellipsoid would put every configured ceiling 200 m out.
    """

    zone_id: str
    name: str
    kind: ZoneKind
    shape: Shape

    floor_up_m: float | None
    ceiling_up_m: float | None

    declaration_order: int
    """Position in the site document. The deterministic tie-break between zones of equal
    severity, so that the site author decides ties rather than dictionary ordering."""

    def signed_distance_m(self, point: EnuPoint) -> float:
        """Horizontal distance to the boundary: negative inside, positive outside.

        Horizontal rather than slant, because a zone's vertical extent is a separate predicate
        with its own bounds. An object 400 m above a protected zone is directly over it, and
        reporting its distance to the boundary as 400 m would describe it as being somewhere
        it is not.
        """
        return self.shape.signed_distance_m(point.east_m, point.north_m)

    def contains_horizontally(self, point: EnuPoint) -> bool:
        """Footprint containment, with boundary contact resolving inwards.

        A point exactly on an edge, or exactly on a vertex, is inside. The rule has to be
        stated because it is otherwise decided by whichever way a floating-point comparison
        falls, and the two resolutions produce different alerts for the same input. Inwards is
        the fail-safe direction: a zone exists to be noticed, and an object grazing the edge of
        a protected area should register as being in it rather than slip through the tie.
        """
        return self.signed_distance_m(point) <= BOUNDARY_TOLERANCE_M

    def contains_altitude(self, up_m: float) -> bool:
        """Whether an altitude falls within the zone's vertical bounds, either of which may be
        absent — an unbounded ceiling is the usual case, since overflight at height is still
        overflight."""
        if self.floor_up_m is not None and up_m < self.floor_up_m:
            return False
        if self.ceiling_up_m is not None and up_m > self.ceiling_up_m:
            return False
        return True

    def contains(self, point: EnuPoint) -> bool:
        return self.contains_horizontally(point) and self.contains_altitude(point.up_m)

    def time_to_enter_s(self, point: EnuPoint, velocity: EnuVelocity) -> float | None:
        """Seconds until a constant-velocity course first satisfies this zone, or None if it
        never does. See `_time_to_enter_s` for the cases that matter."""
        return _time_to_enter_s(self.shape, self.floor_up_m, self.ceiling_up_m, point, velocity)


class SiteGeometry:
    """A site's geography, projected into its own ENU frame and ready to be queried.

    Constructed once per site. The projection and the frame's rotation matrix are computed
    here so that the per-detection path is arithmetic on floats.

    Deliberately clock-free: `time_to_enter_s` returns a duration, not an instant. The caller
    holds the track's timestamp and the injected clock (ADR 0003), and turning "in 42 seconds"
    into a time is its business, not this module's.
    """

    __slots__ = ("_site", "_frame", "_zones", "_protected_area")

    def __init__(self, site: Site) -> None:
        self._site = site
        self._frame = site.local_frame()
        self._zones = tuple(
            _project_zone(zone, order, self._frame, site.reference.altitude_m)
            for order, zone in enumerate(site.zones)
        )
        # The headline protected area is a property of the site itself, independent of whether
        # a protected zone has also been configured, so it is kept as its own footprint rather
        # than looked up among the zones. It is centred on the reference point, which is the
        # origin of the frame by construction.
        self._protected_area = Circle(0.0, 0.0, site.protected_radius_m)

    @property
    def site(self) -> Site:
        return self._site

    @property
    def frame(self) -> LocalFrame:
        return self._frame

    @property
    def zones(self) -> tuple[ProjectedZone, ...]:
        return self._zones

    def to_enu(self, latitude: float, longitude: float, altitude_m: float) -> EnuPoint:
        """A WGS-84 position in this site's frame. Altitude is metres above the ellipsoid."""
        return self._frame.to_enu(GeodeticPoint(latitude, longitude, altitude_m))

    def zone(self, zone_id: str) -> ProjectedZone:
        for zone in self._zones:
            if zone.zone_id == zone_id:
                return zone
        raise KeyError(f"no zone {zone_id!r} in site {self._site.site_id}")

    def zones_containing(self, point: EnuPoint) -> tuple[ProjectedZone, ...]:
        """Every zone the point currently occupies, in declaration order."""
        return tuple(zone for zone in self._zones if zone.contains(point))

    def most_consequential_zone(self, point: EnuPoint) -> ProjectedZone | None:
        """The occupied zone that should drive assessment, or None if the point is in no zone.

        Zones overlap by design — a protected core sits inside a restricted ring inside a
        monitored outer area — so "which zone is it in" has several correct answers and only
        one useful one. `ZoneKind.severity` orders them; declaration order breaks ties, which
        keeps the answer stable across runs and puts the choice in the site author's hands.
        """
        occupied = self.zones_containing(point)
        if not occupied:
            return None
        return max(occupied, key=lambda zone: (zone.kind.severity, -zone.declaration_order))

    def distance_to_protected_area_m(self, point: EnuPoint) -> float:
        """Signed horizontal distance to the site's protected area: negative inside."""
        return self._protected_area.signed_distance_m(point.east_m, point.north_m)

    def time_to_zone_s(
        self, zone_id: str, point: EnuPoint, velocity: EnuVelocity
    ) -> float | None:
        return self.zone(zone_id).time_to_enter_s(point, velocity)

    def time_to_protected_area_s(
        self, point: EnuPoint, velocity: EnuVelocity
    ) -> float | None:
        """Time-to-boundary for the protected area, which risk assessment takes as an input
        whether or not a protected zone is separately configured."""
        return _time_to_enter_s(self._protected_area, None, None, point, velocity)


def _time_to_enter_s(
    shape: Shape,
    floor_up_m: float | None,
    ceiling_up_m: float | None,
    point: EnuPoint,
    velocity: EnuVelocity,
) -> float | None:
    """Seconds until a constant-velocity course first satisfies footprint and altitude at
    once, or None if it never does.

    The naive version of this — distance to the boundary divided by speed — is wrong in every
    interesting case, so each is handled explicitly:

    - **Already inside.** Zero, not None and not the time to the far boundary. The question is
      "when does this reach the zone", and the answer for something already there is now.
    - **Moving away.** The algebra still has a solution; it is in the past. Negative roots are
      discarded rather than taken in absolute value.
    - **Tangential.** A course that grazes the boundary produces a discriminant of zero and a
      single touching instant. That is not an entry, and reporting one would hand the risk
      engine an imminent crossing for an object that never crosses.
    - **Stationary.** No amount of time moves it. Inside stays zero; outside stays None.
    - **Altitude.** Footprint and altitude must hold simultaneously. An object crossing over a
      zone's footprint while still climbing towards its floor has not entered it, and one that
      levels off below the floor never will.
    """
    footprint_windows = _footprint_windows(shape, point, velocity)
    if not footprint_windows:
        return None

    altitude_window = _altitude_window(floor_up_m, ceiling_up_m, point.up_m, velocity.up_mps)
    if altitude_window is None:
        return None

    for start, end in footprint_windows:
        entry = max(start, altitude_window[0], 0.0)
        if entry <= min(end, altitude_window[1]):
            return entry
    return None


def _footprint_windows(
    shape: Shape, point: EnuPoint, velocity: EnuVelocity
) -> list[tuple[float, float]]:
    """Time windows, ascending and disjoint, during which the course is inside the footprint.

    A window may start in the past; callers clamp to zero. A list rather than a single window
    because a straight course can enter and leave a non-convex polygon more than once.
    """
    if isinstance(shape, Circle):
        return _circle_windows(shape, point, velocity)
    return _polygon_windows(shape, point, velocity)


def _circle_windows(
    circle: Circle, point: EnuPoint, velocity: EnuVelocity
) -> list[tuple[float, float]]:
    east = point.east_m - circle.centre_east_m
    north = point.north_m - circle.centre_north_m
    inside_at_start = east * east + north * north - circle.radius_m * circle.radius_m

    if velocity.horizontal_speed_mps <= _STATIONARY_SPEED_MPS:
        return [(0.0, math.inf)] if inside_at_start <= 0.0 else []

    # |p + vt - c|^2 = r^2, solved for t.
    a = velocity.east_mps**2 + velocity.north_mps**2
    b = 2.0 * (east * velocity.east_mps + north * velocity.north_mps)
    discriminant = b * b - 4.0 * a * inside_at_start

    # Strictly greater than zero: a discriminant of exactly zero is the tangential course,
    # which touches the boundary without ever being inside it.
    if discriminant <= 0.0:
        return []

    root = math.sqrt(discriminant)
    return [((-b - root) / (2.0 * a), (-b + root) / (2.0 * a))]


def _polygon_windows(
    polygon: Polygon, point: EnuPoint, velocity: EnuVelocity
) -> list[tuple[float, float]]:
    """Windows for a polygon, found by splitting the course at every edge crossing.

    Containment cannot change anywhere except at a crossing, so the crossing times partition
    the course into intervals of constant containment and a single probe decides each one.
    That handles non-convex footprints, which an entry/exit pair alone does not.

    The probes use strict enclosure, so a course running exactly along an edge is not reported
    as an entry — the same treatment the tangential circle case gets, for the same reason.
    """
    if velocity.horizontal_speed_mps <= _STATIONARY_SPEED_MPS:
        inside = polygon.signed_distance_m(point.east_m, point.north_m) <= BOUNDARY_TOLERANCE_M
        return [(0.0, math.inf)] if inside else []

    crossings = sorted({t for t in _edge_crossing_times(polygon, point, velocity) if t > 0.0})
    starts = [0.0, *crossings]

    windows: list[tuple[float, float]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else math.inf
        probe = start + (1.0 if math.isinf(end) else 0.5 * (end - start))
        if not polygon.encloses(
            point.east_m + velocity.east_mps * probe,
            point.north_m + velocity.north_mps * probe,
        ):
            continue
        if windows and windows[-1][1] == start:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    return windows


def _edge_crossing_times(
    polygon: Polygon, point: EnuPoint, velocity: EnuVelocity
) -> list[float]:
    """Times at which the course crosses an edge segment.

    A course parallel to an edge has no crossing with it, including the collinear case where
    it has infinitely many — neither is a containment change, so both are skipped.
    """
    times: list[float] = []
    for (ax, ay), (bx, by) in polygon.edges():
        edge_east = bx - ax
        edge_north = by - ay
        determinant = velocity.east_mps * edge_north - velocity.north_mps * edge_east
        if determinant == 0.0:
            continue

        offset_east = point.east_m - ax
        offset_north = point.north_m - ay
        along_edge = (
            velocity.east_mps * offset_north - velocity.north_mps * offset_east
        ) / determinant
        if 0.0 <= along_edge <= 1.0:
            times.append((edge_east * offset_north - edge_north * offset_east) / determinant)
    return times


def _altitude_window(
    floor_up_m: float | None,
    ceiling_up_m: float | None,
    up_m: float,
    vertical_rate_mps: float,
) -> tuple[float, float] | None:
    """Time window during which altitude lies within the zone's bounds, or None if never.

    Unbounded above and below is the common case and short-circuits to all time.
    """
    if floor_up_m is None and ceiling_up_m is None:
        return (-math.inf, math.inf)

    if abs(vertical_rate_mps) <= _STATIONARY_SPEED_MPS:
        level_within_bounds = (floor_up_m is None or up_m >= floor_up_m) and (
            ceiling_up_m is None or up_m <= ceiling_up_m
        )
        return (-math.inf, math.inf) if level_within_bounds else None

    start = -math.inf
    end = math.inf
    climbing = vertical_rate_mps > 0.0

    if floor_up_m is not None:
        crossing = (floor_up_m - up_m) / vertical_rate_mps
        if climbing:
            start = max(start, crossing)
        else:
            end = min(end, crossing)

    if ceiling_up_m is not None:
        crossing = (ceiling_up_m - up_m) / vertical_rate_mps
        if climbing:
            end = min(end, crossing)
        else:
            start = max(start, crossing)

    return (start, end) if start <= end else None


def _distance_to_segment_m(
    east_m: float, north_m: float, a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Shortest distance from a point to a line segment.

    The clamp is what distinguishes this from distance to the infinite line: past either end
    of an edge the nearest point on that edge is its vertex, and using the line would report a
    distance to somewhere the boundary does not go.
    """
    segment_east = b[0] - a[0]
    segment_north = b[1] - a[1]
    length_sq = segment_east * segment_east + segment_north * segment_north
    if length_sq == 0.0:
        return math.hypot(east_m - a[0], north_m - a[1])

    projection = ((east_m - a[0]) * segment_east + (north_m - a[1]) * segment_north) / length_sq
    projection = min(1.0, max(0.0, projection))
    return math.hypot(
        east_m - (a[0] + projection * segment_east),
        north_m - (a[1] + projection * segment_north),
    )


def _project_zone(
    zone: Zone, order: int, frame: LocalFrame, reference_altitude_m: float
) -> ProjectedZone:
    if isinstance(zone.geometry, CircularGeometry):
        centre = _project_ground_point(zone.geometry.centre, frame, reference_altitude_m)
        shape: Shape = Circle(centre.east_m, centre.north_m, zone.geometry.radius_m)
    else:
        shape = Polygon(
            tuple(
                (projected.east_m, projected.north_m)
                for projected in (
                    _project_ground_point(vertex, frame, reference_altitude_m)
                    for vertex in zone.geometry.vertices
                )
            )
        )

    return ProjectedZone(
        zone_id=zone.zone_id,
        name=zone.name,
        kind=zone.kind,
        shape=shape,
        floor_up_m=None if zone.floor_m is None else zone.floor_m - reference_altitude_m,
        ceiling_up_m=None if zone.ceiling_m is None else zone.ceiling_m - reference_altitude_m,
        declaration_order=order,
    )


def _project_ground_point(
    vertex: GeoPoint, frame: LocalFrame, reference_altitude_m: float
) -> EnuPoint:
    """Zone geometry has no altitude of its own — it is a footprint — so it is projected at the
    reference altitude and only its east and north components are used."""
    return frame.to_enu(
        GeodeticPoint(vertex.latitude, vertex.longitude, reference_altitude_m)
    )


def load_site(name: str, paths: ConfigPaths | None = None) -> Site:
    """Load a site document from `config/sites/`.

    The site-specific half of configuration loading lives here rather than in
    `backend/common/config.py`, because `common` may not import `backend.zones.site`
    (ADR 0009). `common` supplies the generic load-and-validate machinery; this supplies the
    model and the naming rule.

    The document's `site_id` must match its file name, for the same reason an environment's
    name must: a copied file that was renamed and not edited is otherwise indistinguishable
    from the site it claims to be, and a site reference point is load-bearing configuration.
    """
    paths = paths or ConfigPaths.default()
    path = paths.site_file(name)
    site = load_config(Site, path)

    if site.site_id != name:
        raise ConfigError(
            f"{path}: site_id: declares {site.site_id!r} but was loaded as {name!r}; the file "
            f"name and the site identifier must agree"
        )
    return site


def load_site_for_environment(
    environment: EnvironmentConfig, paths: ConfigPaths | None = None
) -> Site:
    """The site an environment runs against."""
    paths = paths or ConfigPaths.default()
    return load_site(site_config_path(environment, paths).stem, paths)


def load_site_geometry(name: str, paths: ConfigPaths | None = None) -> SiteGeometry:
    """Load a site and project it into its local frame in one step."""
    return SiteGeometry(load_site(name, paths))


def registry_keys_in_use(site: Site, environment: EnvironmentConfig) -> tuple[str, ...]:
    """Sensor registry keys an environment actually needs at a site, sorted and deduplicated.

    Two independent filters, and both matter: a placement disabled in the site document is not
    fitted at all, and a key the environment does not permit is not run here even where it is
    fitted. The sensor layer loads exactly these documents, which is how a simulation profile
    stays out of a run that was not configured for it.
    """
    return tuple(
        sorted(
            {
                placement.registry_key
                for placement in site.sensors
                if placement.enabled and environment.permits_registry_key(placement.registry_key)
            }
        )
    )
