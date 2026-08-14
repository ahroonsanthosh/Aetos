"""Beam geometry: turning a range and an off-boresight angle into a position and an honest
covariance, given how the unit is bolted down.

Pure trigonometry, no hardware knowledge, no site knowledge. It takes what any short-range
radar reports — a slant range, an angle off boresight, and optionally an elevation — plus the
mount's bearing and tilt, and produces an ENU offset from the sensor together with the 3x3
covariance of that offset.

## Why the covariance is the important half

The unit on the bench has **no elevation channel**. It reports where a target is around the
beam, never where it is within the beam's vertical span. There are two ways to handle that:

1. Place the target on the beam centreline and report a position, full stop.
2. Place it on the centreline and report a vertical uncertainty covering the whole beam.

Option 1 is what an integration writes when it is in a hurry, and it is a lie of precision: it
tells the tracker that a target 100 m out is at a known altitude when the beam is 30 degrees
tall and the true altitude spans some 50 m. The filter believes it, weights it accordingly, and
produces confident nonsense. Option 2 tells the truth — centreline is the best estimate, the
error bar is the beam — and lets the tracker weight it as weakly as it deserves. The canonical
schema has `position_covariance_enu` precisely so that a sensor whose error is wildly
anisotropic can say so (`docs/detection_schema.md`), and this is as anisotropic as it gets.

The vertical term is derived from the beam's elevation extent as a uniform distribution across
it, `sigma = extent / sqrt(12)`, because that is exactly what "somewhere in the beam, no further
information" means. It is not a tuning knob dressed as physics.

## Why tilt lives here

Tilting the mount is the only elevation coverage this unit can achieve, and it rotates the whole
cone: upward coverage is bought by giving up ground-level coverage. Nothing about that is a
software feature, and none of it is visible in the unit's output — the reported angle is
identical whether the board is level or pointing at 30 degrees. `mount_tilt_deg` is the one
piece of information that says which slice of sky a return came from, which is why it is
configuration, why it is recorded with every test session, and why it is applied here rather
than being left for a downstream consumer to remember.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from backend.common.geodesy import EnuPoint

_UNIFORM_STD_DIVISOR = math.sqrt(12.0)
"""Standard deviation of a uniform distribution is its width divided by sqrt(12). Used for the
vertical term, where "somewhere in the beam" is precisely a uniform distribution."""


@dataclass(frozen=True, slots=True)
class MountGeometry:
    """How a unit is installed, in the terms the geometry needs.

    A plain value rather than a reference to `MountConfig` so that this module has no dependency
    on the radar configuration schema, and can be reused by any future sensor whose returns are
    range-and-bearing in a tilted frame.
    """

    boresight_deg: float
    """Compass bearing of the beam centre, degrees clockwise from true north."""

    tilt_deg: float
    """Upward tilt of the whole unit from horizontal. Positive is nose-up."""

    height_m: float = 0.0
    """Height of the unit above the site reference altitude."""


@dataclass(frozen=True, slots=True)
class BeamUncertainty:
    """One-sigma error along the three axes of the measurement, in the sensor's own frame."""

    range_sigma_m: float
    azimuth_sigma_deg: float
    elevation_sigma_deg: float
    """Angular uncertainty about the beam plane. For a unit with no elevation channel this is
    the beam's own extent expressed as a standard deviation — the full span of what the sensor
    does not know, not a small measurement error."""


@dataclass(frozen=True, slots=True)
class BeamSolution:
    """Where the target is relative to the sensor, and how well that is known."""

    offset: EnuPoint
    """ENU metres from the sensor, including the mount height on the up axis."""

    covariance_enu: list[list[float]]
    """3x3 position covariance in ENU metres squared, row-major [E, N, U]."""

    slant_range_m: float
    elevation_deg: float
    """Elevation of the beam centreline at this azimuth, above horizontal. Derived from the
    mount tilt, not measured — which is exactly why the covariance carries the vertical span
    the unit could not resolve."""


def unknown_elevation_sigma_deg(beam_elevation_extent_deg: float) -> float:
    """The honest angular uncertainty for a unit that cannot measure elevation.

    Separate and named so that the reasoning is visible at the call site: a target is somewhere
    in a beam of this vertical extent and nothing narrows it further, which is a uniform
    distribution, whose standard deviation is its width over sqrt(12).
    """
    return beam_elevation_extent_deg / _UNIFORM_STD_DIVISOR


def solve_beam(
    *,
    range_m: float,
    azimuth_deg: float,
    mount: MountGeometry,
    uncertainty: BeamUncertainty,
    elevation_deg: float | None = None,
) -> BeamSolution:
    """Resolve one return into an ENU offset from the sensor and its covariance.

    `elevation_deg` is the target's elevation off the beam plane when the unit measures one, and
    None when it does not. None is not treated as zero-with-confidence: the centreline is used
    as the estimate either way, but the caller is expected to have supplied the beam's full
    extent as `elevation_sigma_deg` in that case, and `unknown_elevation_sigma_deg` is what
    computes it.

    Angles are applied in the order the hardware imposes them: azimuth and elevation are
    measured in the *unit's* frame, so they are resolved first, and only then is the whole
    result rotated by the mount's tilt and bearing. Applying the mount rotation first would put
    the azimuth sweep in the wrong plane whenever the tilt is nonzero — a subtle error that is
    invisible at zero tilt and grows with it, so a test campaign that started level would not
    see it until the moment it mattered.
    """
    if range_m < 0.0:
        raise ValueError(f"range_m must not be negative, got {range_m}")

    azimuth = math.radians(azimuth_deg)
    beam_elevation = math.radians(elevation_deg or 0.0)
    tilt = math.radians(mount.tilt_deg)
    bearing = math.radians(mount.boresight_deg)

    # Orthonormal triad of the *untilted* mount, in ENU: forward along the boresight bearing,
    # right at ninety degrees clockwise from it, up vertical.
    forward = (math.sin(bearing), math.cos(bearing), 0.0)
    right = (math.cos(bearing), -math.sin(bearing), 0.0)
    up = (0.0, 0.0, 1.0)

    # Tilt rotates forward and up about the right axis; right itself is the rotation axis and is
    # unchanged. This is what turns a level cone into an elevated one.
    tilted_forward = _combine(forward, math.cos(tilt), up, math.sin(tilt))
    tilted_up = _combine(forward, -math.sin(tilt), up, math.cos(tilt))

    # Line of sight, and the two directions error is measured along. The three are orthonormal,
    # which is what lets the covariance be assembled as a sum of rank-one terms below.
    cos_el, sin_el = math.cos(beam_elevation), math.sin(beam_elevation)
    cos_az, sin_az = math.cos(azimuth), math.sin(azimuth)

    in_plane = _combine(tilted_forward, cos_az, right, sin_az)
    line_of_sight = _combine(in_plane, cos_el, tilted_up, sin_el)
    azimuth_axis = _combine(tilted_forward, -sin_az, right, cos_az)
    elevation_axis = _cross(line_of_sight, azimuth_axis)

    offset = EnuPoint(
        east_m=range_m * line_of_sight[0],
        north_m=range_m * line_of_sight[1],
        up_m=range_m * line_of_sight[2] + mount.height_m,
    )

    # Angular error subtends more metres the further away the target is, so both angular sigmas
    # are converted to metres at this range. The small-angle form is used deliberately: at the
    # angles involved it is accurate to well under a percent, and the exact tangent form would
    # diverge for a beam extent approaching 180 degrees.
    sigma_range = uncertainty.range_sigma_m
    sigma_azimuth = range_m * math.radians(uncertainty.azimuth_sigma_deg)
    sigma_elevation = range_m * math.radians(uncertainty.elevation_sigma_deg)

    covariance = _rank_one(line_of_sight, sigma_range)
    _accumulate(covariance, azimuth_axis, sigma_azimuth)
    _accumulate(covariance, elevation_axis, sigma_elevation)
    _symmetrise(covariance)

    horizontal = math.hypot(line_of_sight[0], line_of_sight[1])
    return BeamSolution(
        offset=offset,
        covariance_enu=covariance,
        slant_range_m=range_m,
        elevation_deg=math.degrees(math.atan2(line_of_sight[2], horizontal)),
    )


Vector = tuple[float, float, float]


def _combine(a: Vector, scale_a: float, b: Vector, scale_b: float) -> Vector:
    return (
        a[0] * scale_a + b[0] * scale_b,
        a[1] * scale_a + b[1] * scale_b,
        a[2] * scale_a + b[2] * scale_b,
    )


def _cross(a: Vector, b: Vector) -> Vector:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _rank_one(axis: Vector, sigma: float) -> list[list[float]]:
    """`sigma^2 * axis axis^T` — the covariance of an error confined to one direction."""
    variance = sigma * sigma
    return [[variance * axis[i] * axis[j] for j in range(3)] for i in range(3)]


def _accumulate(target: list[list[float]], axis: Vector, sigma: float) -> None:
    variance = sigma * sigma
    for i in range(3):
        for j in range(3):
            target[i][j] += variance * axis[i] * axis[j]


def _symmetrise(matrix: list[list[float]]) -> None:
    """Force exact symmetry.

    The construction above is symmetric in exact arithmetic and symmetric to within rounding in
    floating point. `Measurement.position_covariance_enu` validates symmetry, and a detection
    rejected by the schema over a difference in the fifteenth decimal place would be an
    absurd way to lose a return.
    """
    for i in range(3):
        for j in range(i + 1, 3):
            mean = 0.5 * (matrix[i][j] + matrix[j][i])
            matrix[i][j] = mean
            matrix[j][i] = mean
