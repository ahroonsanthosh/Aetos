"""WGS-84 <-> local ENU conversion.

The canonical schema and the API speak WGS-84 degrees. Everything between them works in a
local East-North-Up tangent plane in metres, because degrees are not a metric frame: a degree
of longitude shrinks with the cosine of latitude, so a Kalman filter or an association gate
run directly on degrees produces uncertainty that is wrong, and wrong by a different factor
in each axis.

Implemented directly against the WGS-84 ellipsoid rather than taking a geodetic dependency —
the geodetic-to-ECEF-to-ENU chain is short, exact, and worth having visible.

See docs/adr/0004.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# WGS-84 defining parameters.
_SEMI_MAJOR_AXIS_M = 6_378_137.0
_FLATTENING = 1.0 / 298.257_223_563
_SEMI_MINOR_AXIS_M = _SEMI_MAJOR_AXIS_M * (1.0 - _FLATTENING)
_ECCENTRICITY_SQ = _FLATTENING * (2.0 - _FLATTENING)


@dataclass(frozen=True, slots=True)
class GeodeticPoint:
    """WGS-84 position. Altitude is metres above the ellipsoid."""

    latitude_deg: float
    longitude_deg: float
    altitude_m: float


@dataclass(frozen=True, slots=True)
class EnuPoint:
    """Local tangent-plane position in metres, relative to an origin."""

    east_m: float
    north_m: float
    up_m: float


def geodetic_to_ecef(point: GeodeticPoint) -> tuple[float, float, float]:
    """WGS-84 geodetic to Earth-Centred Earth-Fixed Cartesian metres."""
    lat = math.radians(point.latitude_deg)
    lon = math.radians(point.longitude_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)

    # Radius of curvature in the prime vertical.
    n = _SEMI_MAJOR_AXIS_M / math.sqrt(1.0 - _ECCENTRICITY_SQ * sin_lat * sin_lat)

    x = (n + point.altitude_m) * cos_lat * math.cos(lon)
    y = (n + point.altitude_m) * cos_lat * math.sin(lon)
    z = (n * (1.0 - _ECCENTRICITY_SQ) + point.altitude_m) * sin_lat
    return x, y, z


def ecef_to_geodetic(x: float, y: float, z: float) -> GeodeticPoint:
    """ECEF Cartesian metres to WGS-84 geodetic.

    Uses Bowring's method, which converges to sub-millimetre accuracy for terrestrial and
    near-Earth altitudes without iteration. That is far below any sensor uncertainty we will
    ever see, so a closed form is preferred to an iterative solve.
    """
    lon = math.atan2(y, x)
    p = math.hypot(x, y)

    if p < 1e-9:
        # On the polar axis: longitude is undefined, latitude is +/-90.
        lat = math.copysign(math.pi / 2.0, z)
        altitude = abs(z) - _SEMI_MINOR_AXIS_M
        return GeodeticPoint(math.degrees(lat), math.degrees(lon), altitude)

    # Second eccentricity squared, used by Bowring's auxiliary latitude.
    e_prime_sq = (_SEMI_MAJOR_AXIS_M**2 - _SEMI_MINOR_AXIS_M**2) / _SEMI_MINOR_AXIS_M**2
    theta = math.atan2(z * _SEMI_MAJOR_AXIS_M, p * _SEMI_MINOR_AXIS_M)

    lat = math.atan2(
        z + e_prime_sq * _SEMI_MINOR_AXIS_M * math.sin(theta) ** 3,
        p - _ECCENTRICITY_SQ * _SEMI_MAJOR_AXIS_M * math.cos(theta) ** 3,
    )

    sin_lat = math.sin(lat)
    n = _SEMI_MAJOR_AXIS_M / math.sqrt(1.0 - _ECCENTRICITY_SQ * sin_lat * sin_lat)
    altitude = p / math.cos(lat) - n

    return GeodeticPoint(math.degrees(lat), math.degrees(lon), altitude)


class LocalFrame:
    """A local ENU tangent plane anchored at a fixed geodetic origin.

    Constructed once per site from the site reference point and then used for every
    conversion, so the rotation matrix is computed once rather than per detection.

    The origin is immutable for the lifetime of a running system. Changing it would silently
    invalidate every in-flight track's state, since track positions are stored in this frame.
    """

    __slots__ = ("_origin", "_origin_ecef", "_sin_lat", "_cos_lat", "_sin_lon", "_cos_lon")

    def __init__(self, origin: GeodeticPoint) -> None:
        self._origin = origin
        self._origin_ecef = geodetic_to_ecef(origin)
        lat = math.radians(origin.latitude_deg)
        lon = math.radians(origin.longitude_deg)
        self._sin_lat = math.sin(lat)
        self._cos_lat = math.cos(lat)
        self._sin_lon = math.sin(lon)
        self._cos_lon = math.cos(lon)

    @property
    def origin(self) -> GeodeticPoint:
        return self._origin

    def to_enu(self, point: GeodeticPoint) -> EnuPoint:
        """Geodetic position to local ENU metres."""
        x, y, z = geodetic_to_ecef(point)
        dx = x - self._origin_ecef[0]
        dy = y - self._origin_ecef[1]
        dz = z - self._origin_ecef[2]

        sin_lat, cos_lat = self._sin_lat, self._cos_lat
        sin_lon, cos_lon = self._sin_lon, self._cos_lon

        east = -sin_lon * dx + cos_lon * dy
        north = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
        up = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz
        return EnuPoint(east, north, up)

    def to_geodetic(self, point: EnuPoint) -> GeodeticPoint:
        """Local ENU metres back to geodetic position."""
        e, n, u = point.east_m, point.north_m, point.up_m

        sin_lat, cos_lat = self._sin_lat, self._cos_lat
        sin_lon, cos_lon = self._sin_lon, self._cos_lon

        # Transpose of the ENU rotation matrix applied in to_enu.
        dx = -sin_lon * e - sin_lat * cos_lon * n + cos_lat * cos_lon * u
        dy = cos_lon * e - sin_lat * sin_lon * n + cos_lat * sin_lon * u
        dz = cos_lat * n + sin_lat * u

        return ecef_to_geodetic(
            self._origin_ecef[0] + dx,
            self._origin_ecef[1] + dy,
            self._origin_ecef[2] + dz,
        )


def horizontal_distance_m(a: EnuPoint, b: EnuPoint) -> float:
    """Ground-plane distance between two ENU points, ignoring altitude."""
    return math.hypot(b.east_m - a.east_m, b.north_m - a.north_m)


def slant_distance_m(a: EnuPoint, b: EnuPoint) -> float:
    """Three-dimensional distance between two ENU points."""
    return math.sqrt(
        (b.east_m - a.east_m) ** 2 + (b.north_m - a.north_m) ** 2 + (b.up_m - a.up_m) ** 2
    )


def bearing_deg(from_point: EnuPoint, to_point: EnuPoint) -> float:
    """Compass bearing from one ENU point to another, degrees clockwise from north."""
    east = to_point.east_m - from_point.east_m
    north = to_point.north_m - from_point.north_m
    return math.degrees(math.atan2(east, north)) % 360.0
