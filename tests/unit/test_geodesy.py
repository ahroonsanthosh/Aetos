"""Geodesy: round-trip fidelity, frame orientation, and the limits of the tangent plane."""

from __future__ import annotations

import math

import pytest

from backend.common.geodesy import (
    EnuPoint,
    GeodeticPoint,
    LocalFrame,
    bearing_deg,
    ecef_to_geodetic,
    geodetic_to_ecef,
    horizontal_distance_m,
    slant_distance_m,
)

DUBLIN = GeodeticPoint(53.35, -6.26, 0.0)


@pytest.mark.parametrize(
    "point",
    [
        GeodeticPoint(53.35, -6.26, 0.0),
        GeodeticPoint(0.0, 0.0, 0.0),
        GeodeticPoint(-33.87, 151.21, 250.0),
        GeodeticPoint(64.14, -21.94, 1200.0),
        GeodeticPoint(1.35, 103.82, -15.0),
    ],
)
def test_ecef_round_trip_is_sub_millimetre(point: GeodeticPoint) -> None:
    """Geodetic -> ECEF -> geodetic must not lose precision that matters.

    Bowring's method is a closed form, so error here would indicate an implementation
    mistake rather than accumulated iteration error.
    """
    x, y, z = geodetic_to_ecef(point)
    back = ecef_to_geodetic(x, y, z)

    assert back.latitude_deg == pytest.approx(point.latitude_deg, abs=1e-9)
    assert back.longitude_deg == pytest.approx(point.longitude_deg, abs=1e-9)
    assert back.altitude_m == pytest.approx(point.altitude_m, abs=1e-4)


def test_enu_round_trip_preserves_position() -> None:
    frame = LocalFrame(DUBLIN)
    original = GeodeticPoint(53.3608, -6.2503, 120.0)

    enu = frame.to_enu(original)
    back = frame.to_geodetic(enu)

    assert back.latitude_deg == pytest.approx(original.latitude_deg, abs=1e-9)
    assert back.longitude_deg == pytest.approx(original.longitude_deg, abs=1e-9)
    assert back.altitude_m == pytest.approx(original.altitude_m, abs=1e-4)


def test_origin_maps_to_enu_zero() -> None:
    frame = LocalFrame(DUBLIN)
    enu = frame.to_enu(DUBLIN)

    assert enu.east_m == pytest.approx(0.0, abs=1e-6)
    assert enu.north_m == pytest.approx(0.0, abs=1e-6)
    assert enu.up_m == pytest.approx(0.0, abs=1e-6)


def test_axes_point_the_right_way() -> None:
    """East is +east, north is +north, up is +up. A sign error here would silently mirror
    the entire tactical picture, so it is worth asserting explicitly."""
    frame = LocalFrame(DUBLIN)

    north_of = frame.to_enu(GeodeticPoint(DUBLIN.latitude_deg + 0.01, DUBLIN.longitude_deg, 0.0))
    assert north_of.north_m > 0.0
    assert abs(north_of.east_m) < 1.0

    east_of = frame.to_enu(GeodeticPoint(DUBLIN.latitude_deg, DUBLIN.longitude_deg + 0.01, 0.0))
    assert east_of.east_m > 0.0
    assert abs(east_of.north_m) < 1.0

    above = frame.to_enu(GeodeticPoint(DUBLIN.latitude_deg, DUBLIN.longitude_deg, 500.0))
    assert above.up_m == pytest.approx(500.0, abs=1e-3)


def test_longitude_degree_shrinks_with_latitude() -> None:
    """The reason tracking maths cannot run in degrees.

    One degree of longitude is roughly 66 km at Dublin's latitude and roughly 111 km at the
    equator. Any gate or filter working directly in degrees inherits this distortion.
    """
    equator = LocalFrame(GeodeticPoint(0.0, 0.0, 0.0))
    at_dublin = LocalFrame(DUBLIN).to_enu(GeodeticPoint(53.35, -5.26, 0.0)).east_m
    at_equator = equator.to_enu(GeodeticPoint(0.0, 1.0, 0.0)).east_m

    assert at_dublin < at_equator * 0.65
    assert at_equator == pytest.approx(111_319.0, rel=0.01)


def test_tangent_plane_error_is_negligible_at_cuas_ranges() -> None:
    """FM-002: the tangent-plane approximation bounds site extent.

    At the ranges Aetos operates over — protected radius on the order of a kilometre, sensor
    ranges in the low tens of kilometres — the round-trip error must be far below sensor
    measurement uncertainty, which is metres at best.
    """
    frame = LocalFrame(DUBLIN)

    for range_m in (1_000.0, 5_000.0, 20_000.0, 50_000.0):
        for bearing in (0.0, 45.0, 90.0, 180.0, 270.0):
            enu = EnuPoint(
                east_m=range_m * math.sin(math.radians(bearing)),
                north_m=range_m * math.cos(math.radians(bearing)),
                up_m=150.0,
            )
            recovered = frame.to_enu(frame.to_geodetic(enu))
            error = slant_distance_m(enu, recovered)
            assert error < 0.01, f"round-trip error {error:.4f} m at {range_m} m, {bearing} deg"


def test_horizontal_distance_ignores_altitude() -> None:
    a = EnuPoint(0.0, 0.0, 0.0)
    b = EnuPoint(300.0, 400.0, 5_000.0)

    assert horizontal_distance_m(a, b) == pytest.approx(500.0)
    assert slant_distance_m(a, b) == pytest.approx(math.sqrt(500.0**2 + 5_000.0**2))


@pytest.mark.parametrize(
    ("east", "north", "expected"),
    [(0.0, 100.0, 0.0), (100.0, 0.0, 90.0), (0.0, -100.0, 180.0), (-100.0, 0.0, 270.0)],
)
def test_bearing_is_clockwise_from_north(east: float, north: float, expected: float) -> None:
    assert bearing_deg(EnuPoint(0.0, 0.0, 0.0), EnuPoint(east, north, 0.0)) == pytest.approx(
        expected
    )
