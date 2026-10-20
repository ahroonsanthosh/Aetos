"""Geofencing and zone-crossing prediction.

Two kinds of test here. The geometry cases build shapes directly in ENU metres, because "the
point is exactly on the edge" has to mean exactly, and a coordinate round-tripped through
WGS-84 lands a few millimetres off. The site-level cases use the shipped fictional site, so
that the configuration actually loads and projects rather than merely parsing.

Zones and sites are constructed in code here. That is a test fixture, not a licence: no site,
coordinate or threshold appears in application code, which is what
`test_the_shipped_site_config_loads_and_projects` exists to keep honest.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from backend.common.config import ConfigError, ConfigPaths, load_environment
from backend.common.geodesy import EnuPoint
from backend.zones.geometry import (
    Circle,
    EnuVelocity,
    Polygon,
    ProjectedZone,
    SiteGeometry,
    load_site,
    load_site_for_environment,
    load_site_geometry,
    registry_keys_in_use,
)
from backend.zones.site import (
    CircularGeometry,
    GeodeticReference,
    GeoPoint,
    Site,
    Zone,
    ZoneKind,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_PATHS = ConfigPaths(repo_root=REPO_ROOT, config_root=REPO_ROOT / "config")
SHIPPED_SITE = "fictional_test_range"

UNIT_CIRCLE = Circle(0.0, 0.0, 100.0)
SQUARE = Polygon(((0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)))

# A C opening east: at north 150 the footprint spans east 0 to 100 only, and a course running
# north up east 200 leaves and re-enters it. Convex-only reasoning gets this wrong.
C_SHAPE = Polygon(
    (
        (0.0, 0.0),
        (300.0, 0.0),
        (300.0, 100.0),
        (100.0, 100.0),
        (100.0, 200.0),
        (300.0, 200.0),
        (300.0, 300.0),
        (0.0, 300.0),
    )
)


def _zone(
    shape: Circle | Polygon,
    *,
    kind: ZoneKind = ZoneKind.RESTRICTED,
    floor_up_m: float | None = None,
    ceiling_up_m: float | None = None,
    zone_id: str = "fixture_zone",
    order: int = 0,
) -> ProjectedZone:
    return ProjectedZone(
        zone_id=zone_id,
        name="Fixture zone",
        kind=kind,
        shape=shape,
        floor_up_m=floor_up_m,
        ceiling_up_m=ceiling_up_m,
        declaration_order=order,
    )


def _at(east: float, north: float, up: float = 0.0) -> EnuPoint:
    return EnuPoint(east, north, up)


def _synthetic_site(*zones: Zone) -> Site:
    return Site(
        site_id="fixture_site",
        name="Fixture site",
        reference=GeodeticReference(latitude=45.0, longitude=0.0, altitude_m=0.0),
        protected_radius_m=500.0,
        zones=list(zones),
    )


def _circular_zone(zone_id: str, kind: ZoneKind, radius_m: float) -> Zone:
    return Zone(
        zone_id=zone_id,
        name=zone_id,
        kind=kind,
        geometry=CircularGeometry(centre=GeoPoint(latitude=45.0, longitude=0.0), radius_m=radius_m),
    )


# --- containment ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        (_at(0.0, 0.0), True),
        (_at(99.0, 0.0), True),
        (_at(70.0, 70.0), True),
        (_at(101.0, 0.0), False),
        (_at(80.0, 80.0), False),
    ],
)
def test_point_in_circle(point: EnuPoint, expected: bool) -> None:
    assert _zone(UNIT_CIRCLE).contains(point) is expected


@pytest.mark.parametrize(
    "point", [_at(100.0, 0.0), _at(-100.0, 0.0), _at(0.0, 100.0), _at(0.0, -100.0)]
)
def test_a_point_exactly_on_a_circle_boundary_is_inside(point: EnuPoint) -> None:
    """Boundary contact resolves inwards. A zone exists to be noticed, so something grazing the
    edge of one should register rather than slip through a floating-point tie."""
    assert _zone(UNIT_CIRCLE).contains(point)


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        (_at(50.0, 50.0), True),
        (_at(0.1, 0.1), True),
        (_at(99.9, 99.9), True),
        (_at(-0.1, 50.0), False),
        (_at(150.0, 50.0), False),
        (_at(50.0, -0.1), False),
    ],
)
def test_point_in_polygon(point: EnuPoint, expected: bool) -> None:
    assert _zone(SQUARE).contains(point) is expected


@pytest.mark.parametrize(
    "point",
    [_at(50.0, 0.0), _at(100.0, 50.0), _at(50.0, 100.0), _at(0.0, 50.0)],
)
def test_a_point_exactly_on_a_polygon_edge_is_inside(point: EnuPoint) -> None:
    """The case a crossing-number test alone cannot decide: the count is ill-defined on the
    boundary, so containment is taken from the distance instead, which is not."""
    assert _zone(SQUARE).contains(point)


@pytest.mark.parametrize("vertex", SQUARE.vertices)
def test_a_point_exactly_on_a_polygon_vertex_is_inside(vertex: tuple[float, float]) -> None:
    """A ray through a vertex is the classic double-count, and the naive fix turns it into a
    zero-count instead. Both are wrong for the same input, so the rule is stated rather than
    inherited from whichever comparison happens to run first."""
    assert _zone(SQUARE).contains(_at(*vertex))


@pytest.mark.parametrize(
    ("point", "expected"),
    [(_at(50.0, 150.0), True), (_at(200.0, 150.0), False), (_at(200.0, 50.0), True)],
)
def test_a_non_convex_footprint_is_handled(point: EnuPoint, expected: bool) -> None:
    assert _zone(C_SHAPE).contains(point) is expected


# --- altitude bounds --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("up_m", "expected"),
    [(49.0, False), (50.0, True), (250.0, True), (500.0, True), (501.0, False)],
)
def test_altitude_bounds_are_honoured_and_inclusive(up_m: float, expected: bool) -> None:
    zone = _zone(UNIT_CIRCLE, floor_up_m=50.0, ceiling_up_m=500.0)

    assert zone.contains(_at(0.0, 0.0, up_m)) is expected
    assert zone.contains_horizontally(_at(0.0, 0.0, up_m))


def test_an_unbounded_ceiling_admits_any_altitude() -> None:
    """The usual case: something overflying a protected area at height is still overflying
    it."""
    zone = _zone(UNIT_CIRCLE)

    assert zone.contains(_at(0.0, 0.0, 12_000.0))


# --- signed distance --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        (_at(0.0, 0.0), -100.0),
        (_at(60.0, 0.0), -40.0),
        (_at(100.0, 0.0), 0.0),
        (_at(250.0, 0.0), 150.0),
        (_at(300.0, 400.0), 400.0),
    ],
)
def test_signed_distance_to_a_circle(point: EnuPoint, expected: float) -> None:
    assert _zone(UNIT_CIRCLE).signed_distance_m(point) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        (_at(50.0, 50.0), -50.0),
        (_at(10.0, 50.0), -10.0),
        (_at(50.0, 0.0), 0.0),
        (_at(150.0, 50.0), 50.0),
        (_at(-30.0, -40.0), 50.0),
    ],
)
def test_signed_distance_to_a_polygon(point: EnuPoint, expected: float) -> None:
    """Negative inside, positive outside. The last case sits diagonally off a corner, where
    distance to the nearest edge *line* would be wrong and distance to the vertex is right."""
    assert _zone(SQUARE).signed_distance_m(point) == pytest.approx(expected)


def test_signed_distance_ignores_altitude() -> None:
    """Distance to a boundary is a horizontal question. Reporting an object 400 m above a
    protected zone as 400 m from it would describe it as somewhere it is not."""
    zone = _zone(UNIT_CIRCLE, ceiling_up_m=100.0)

    assert zone.signed_distance_m(_at(200.0, 0.0, 4_000.0)) == pytest.approx(100.0)


def test_distance_to_the_protected_area() -> None:
    geometry = SiteGeometry(_synthetic_site())

    assert geometry.distance_to_protected_area_m(_at(0.0, 0.0)) == pytest.approx(-500.0)
    assert geometry.distance_to_protected_area_m(_at(1_200.0, 0.0)) == pytest.approx(700.0)


# --- time to boundary -------------------------------------------------------------------


def test_time_to_boundary_when_approaching_head_on() -> None:
    zone = _zone(UNIT_CIRCLE)

    time_s = zone.time_to_enter_s(_at(-500.0, 0.0), EnuVelocity(10.0, 0.0))

    assert time_s == pytest.approx(40.0)


def test_time_to_boundary_on_an_oblique_course() -> None:
    """Not distance-to-centre over speed: the course clips the edge, so the entry point is the
    near root of the intercept, not the closest approach."""
    zone = _zone(UNIT_CIRCLE)

    time_s = zone.time_to_enter_s(_at(-500.0, 50.0), EnuVelocity(10.0, 0.0))

    assert time_s == pytest.approx((500.0 - math.sqrt(100.0**2 - 50.0**2)) / 10.0)


def test_time_to_boundary_is_none_when_moving_away() -> None:
    """The algebra still has a solution here. It is in the past, and taking its magnitude would
    report an object leaving at speed as one about to arrive."""
    zone = _zone(UNIT_CIRCLE)

    assert zone.time_to_enter_s(_at(-500.0, 0.0), EnuVelocity(-10.0, 0.0)) is None


def test_time_to_boundary_is_none_on_a_tangential_course() -> None:
    """A course grazing the boundary touches it at one instant and is never inside. Reporting an
    entry would hand the risk engine an imminent crossing for an object that never crosses."""
    zone = _zone(UNIT_CIRCLE)

    assert zone.time_to_enter_s(_at(-500.0, 100.0), EnuVelocity(10.0, 0.0)) is None


def test_time_to_boundary_is_zero_when_already_inside() -> None:
    """The question is when it reaches the zone. Something already there reaches it now — not
    None, and not the time to the far side."""
    zone = _zone(UNIT_CIRCLE)

    assert zone.time_to_enter_s(_at(10.0, 0.0), EnuVelocity(10.0, 0.0)) == 0.0


def test_a_stationary_object_never_arrives_but_may_already_be_there() -> None:
    zone = _zone(UNIT_CIRCLE)
    still = EnuVelocity(0.0, 0.0, 0.0)

    assert zone.time_to_enter_s(_at(-500.0, 0.0), still) is None
    assert zone.time_to_enter_s(_at(0.0, 0.0), still) == 0.0


def test_time_to_boundary_waits_for_the_altitude_band() -> None:
    """Footprint and altitude must hold at the same instant. This course is over the footprint
    from the start and still climbing towards the floor."""
    zone = _zone(UNIT_CIRCLE, floor_up_m=100.0, ceiling_up_m=500.0)

    time_s = zone.time_to_enter_s(_at(0.0, 0.0, 0.0), EnuVelocity(0.0, 0.0, 10.0))

    assert time_s == pytest.approx(10.0)


def test_time_to_boundary_takes_the_later_of_footprint_and_altitude() -> None:
    zone = _zone(UNIT_CIRCLE, floor_up_m=100.0, ceiling_up_m=500.0)

    time_s = zone.time_to_enter_s(_at(-500.0, 0.0, 0.0), EnuVelocity(10.0, 0.0, 10.0))

    assert time_s == pytest.approx(40.0)


def test_a_course_that_crosses_the_footprint_below_the_floor_never_enters() -> None:
    """Climbing, but not fast enough: it is past the far edge before it reaches the floor."""
    zone = _zone(UNIT_CIRCLE, floor_up_m=100.0, ceiling_up_m=500.0)

    assert zone.time_to_enter_s(_at(-500.0, 0.0, 0.0), EnuVelocity(10.0, 0.0, 1.0)) is None


def test_level_flight_below_the_floor_never_enters() -> None:
    zone = _zone(UNIT_CIRCLE, floor_up_m=100.0)

    assert zone.time_to_enter_s(_at(-500.0, 0.0, 20.0), EnuVelocity(10.0, 0.0, 0.0)) is None


def test_time_to_boundary_for_a_polygon() -> None:
    zone = _zone(SQUARE)

    time_s = zone.time_to_enter_s(_at(-200.0, 50.0), EnuVelocity(4.0, 0.0))

    assert time_s == pytest.approx(50.0)


def test_a_non_convex_footprint_can_be_entered_more_than_once() -> None:
    """Up east 200 the C is entered, left and entered again. A single entry/exit pair would
    describe a different shape."""
    zone = _zone(C_SHAPE)

    assert zone.time_to_enter_s(_at(200.0, -100.0), EnuVelocity(0.0, 10.0)) == pytest.approx(10.0)


def test_a_course_starting_in_a_notch_takes_the_next_window_not_the_past_one() -> None:
    """Starting inside the C's notch, the first crossing window is behind it. Clamping the
    earliest window to zero rather than discarding it would report an immediate entry."""
    zone = _zone(C_SHAPE)

    assert zone.time_to_enter_s(_at(200.0, 150.0), EnuVelocity(0.0, 10.0)) == pytest.approx(5.0)


def test_time_to_the_protected_area() -> None:
    geometry = SiteGeometry(_synthetic_site())

    approaching = geometry.time_to_protected_area_s(_at(-1_000.0, 0.0), EnuVelocity(25.0, 0.0))
    receding = geometry.time_to_protected_area_s(_at(-1_000.0, 0.0), EnuVelocity(-25.0, 0.0))

    assert approaching == pytest.approx(20.0)
    assert receding is None


# --- which zone matters -----------------------------------------------------------------


def test_the_most_consequential_zone_wins_where_zones_overlap() -> None:
    """Overlap is the design, not an edge case: a protected core sits inside a restricted ring
    inside a monitored outer area. "Which zone" has three right answers and one useful one."""
    geometry = SiteGeometry(
        _synthetic_site(
            _circular_zone("outer", ZoneKind.MONITORED, 8_000.0),
            _circular_zone("middle", ZoneKind.RESTRICTED, 2_500.0),
            _circular_zone("core", ZoneKind.PROTECTED, 800.0),
        )
    )

    assert [z.zone_id for z in geometry.zones_containing(_at(0.0, 0.0))] == [
        "outer",
        "middle",
        "core",
    ]
    assert geometry.most_consequential_zone(_at(0.0, 0.0)).zone_id == "core"
    assert geometry.most_consequential_zone(_at(0.0, 1_500.0)).zone_id == "middle"
    assert geometry.most_consequential_zone(_at(0.0, 5_000.0)).zone_id == "outer"
    assert geometry.most_consequential_zone(_at(0.0, 20_000.0)) is None


def test_equal_severity_is_broken_by_declaration_order() -> None:
    """Ties go to the zone declared first, so the site author decides them and the answer does
    not change between runs."""
    first = _circular_zone("first_declared", ZoneKind.RESTRICTED, 2_000.0)
    second = _circular_zone("second_declared", ZoneKind.RESTRICTED, 2_000.0)

    assert (
        SiteGeometry(_synthetic_site(first, second)).most_consequential_zone(_at(0.0, 0.0))
    ).zone_id == "first_declared"
    assert (
        SiteGeometry(_synthetic_site(second, first)).most_consequential_zone(_at(0.0, 0.0))
    ).zone_id == "second_declared"


# --- the shipped configuration ----------------------------------------------------------


def test_the_shipped_site_config_loads_and_projects() -> None:
    geometry = load_site_geometry(SHIPPED_SITE, REPO_PATHS)

    assert geometry.site.site_id == SHIPPED_SITE
    assert {zone.zone_id for zone in geometry.zones} == {
        "core_protected",
        "inner_restricted",
        "outer_monitored",
        "eastern_approach_corridor",
    }
    assert geometry.site.sensors
    assert {s.registry_key for s in geometry.site.sensors} == {
        "sim_radar_medium",
        "sim_eo_ir_narrow",
        "sim_rf_wide",
    }


def test_a_wgs84_position_is_projected_by_the_site_before_it_is_tested() -> None:
    """Zone questions are answered in ENU metres, so the conversion belongs to the site rather
    than to every caller that has a latitude and a longitude to hand."""
    geometry = load_site_geometry(SHIPPED_SITE, REPO_PATHS)
    reference = geometry.site.reference

    overhead = geometry.to_enu(
        reference.latitude, reference.longitude, reference.altitude_m + 100.0
    )

    assert geometry.frame.origin.latitude_deg == pytest.approx(reference.latitude)
    assert overhead.up_m == pytest.approx(100.0, abs=1e-3)
    assert geometry.most_consequential_zone(overhead).zone_id == "core_protected"


def test_time_to_a_named_zone_on_the_shipped_site() -> None:
    geometry = load_site_geometry(SHIPPED_SITE, REPO_PATHS)
    inbound = _at(3_000.0, 0.0, 150.0)

    time_s = geometry.time_to_zone_s("core_protected", inbound, EnuVelocity(-20.0, 0.0))

    assert time_s == pytest.approx((3_000.0 - geometry.site.protected_radius_m) / 20.0)


def test_zone_altitude_bounds_are_translated_into_the_local_frame() -> None:
    """Configured bounds are ellipsoidal metres; ENU `up_m` is measured from the site reference
    altitude. A site 120 m above the ellipsoid would otherwise have every ceiling 120 m out —
    FM-004 in miniature."""
    geometry = load_site_geometry(SHIPPED_SITE, REPO_PATHS)
    corridor = geometry.zone("eastern_approach_corridor")
    reference_altitude_m = geometry.site.reference.altitude_m

    assert reference_altitude_m > 0.0
    assert corridor.floor_up_m == pytest.approx(170.0 - reference_altitude_m)
    assert corridor.ceiling_up_m == pytest.approx(620.0 - reference_altitude_m)


def test_the_shipped_corridor_is_a_slab_not_a_column() -> None:
    geometry = load_site_geometry(SHIPPED_SITE, REPO_PATHS)

    in_the_corridor = _at(4_000.0, 0.0, 300.0)
    above_the_corridor = _at(4_000.0, 0.0, 1_200.0)

    assert geometry.most_consequential_zone(in_the_corridor).zone_id == (
        "eastern_approach_corridor"
    )
    assert geometry.most_consequential_zone(above_the_corridor).zone_id == "outer_monitored"


def test_the_shipped_site_protected_radius_matches_its_protected_zone() -> None:
    geometry = load_site_geometry(SHIPPED_SITE, REPO_PATHS)

    on_the_protected_boundary = _at(geometry.site.protected_radius_m, 0.0, 50.0)

    assert geometry.distance_to_protected_area_m(on_the_protected_boundary) == pytest.approx(0.0)
    assert geometry.zone("core_protected").contains(on_the_protected_boundary)


def test_the_site_named_by_an_environment_is_the_one_that_loads() -> None:
    environment = load_environment("sim", REPO_PATHS)

    assert load_site_for_environment(environment, REPO_PATHS).site_id == environment.site


def test_registry_keys_in_use_filters_by_placement_and_by_environment() -> None:
    """Both filters count: a disabled placement is not fitted, and a key the environment does
    not permit does not run here even where it is fitted."""
    site = load_site(SHIPPED_SITE, REPO_PATHS)
    unrestricted = load_environment("sim", REPO_PATHS)

    assert registry_keys_in_use(site, unrestricted) == (
        "sim_eo_ir_narrow",
        "sim_radar_medium",
        "sim_rf_wide",
    )

    radar_only = unrestricted.model_copy(update={"enabled_sensor_keys": ["sim_radar_medium"]})
    assert registry_keys_in_use(site, radar_only) == ("sim_radar_medium",)

    disabled = site.model_copy(
        update={"sensors": [s for s in site.sensors if s.registry_key != "sim_rf_wide"]}
    )
    assert "sim_rf_wide" not in registry_keys_in_use(disabled, unrestricted)


def test_a_site_document_must_declare_the_identifier_its_file_name_claims(
    tmp_path: Path,
) -> None:
    """A site reference point anchors every track's coordinate frame. A renamed copy loading
    silently under the wrong name is not a cosmetic mistake."""
    paths = ConfigPaths(repo_root=tmp_path, config_root=tmp_path / "config")
    paths.sites.mkdir(parents=True)
    shipped = (REPO_PATHS.sites / f"{SHIPPED_SITE}.json").read_text(encoding="utf-8")
    (paths.sites / "renamed_site.json").write_text(shipped, encoding="utf-8")

    with pytest.raises(ConfigError) as raised:
        load_site("renamed_site", paths)

    assert "renamed_site.json" in str(raised.value)
    assert "site_id" in str(raised.value)


def test_a_malformed_site_fails_loudly_naming_the_file(tmp_path: Path) -> None:
    paths = ConfigPaths(repo_root=tmp_path, config_root=tmp_path / "config")
    paths.sites.mkdir(parents=True)
    (paths.sites / "broken.json").write_text(
        '{"site_id": "broken", "name": "Broken", "protected_radius_m": -1.0, '
        '"reference": {"latitude": 45.0, "longitude": 0.0}}',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as raised:
        load_site("broken", paths)

    message = str(raised.value)
    assert "broken.json" in message
    assert "protected_radius_m" in message
