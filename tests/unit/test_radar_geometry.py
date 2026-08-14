"""Beam geometry: mount tilt, bearing, and the covariance of what the unit cannot see.

The tilt cases matter more than they look. Applying the mount rotation in the wrong order is
invisible at zero tilt and grows with it, so a campaign that started level would not see the
error until the moment elevation coverage was the whole point. These tests fix the order.
"""

from __future__ import annotations

import math

import pytest

from sensors.radar.geometry import (
    BeamUncertainty,
    MountGeometry,
    solve_beam,
    unknown_elevation_sigma_deg,
)

TIGHT = BeamUncertainty(range_sigma_m=1.0, azimuth_sigma_deg=0.0, elevation_sigma_deg=0.0)
"""Range error only. Used where the assertion is about position, so that the covariance's
directions do not have to be reasoned about at the same time."""


def solve(**kwargs: object):
    defaults = {"mount": MountGeometry(boresight_deg=0.0, tilt_deg=0.0), "uncertainty": TIGHT}
    defaults.update(kwargs)
    return solve_beam(**defaults)  # type: ignore[arg-type]


class TestPosition:
    def test_boresight_north_puts_a_zero_azimuth_target_due_north(self) -> None:
        solution = solve(range_m=100.0, azimuth_deg=0.0)

        assert solution.offset.north_m == pytest.approx(100.0)
        assert solution.offset.east_m == pytest.approx(0.0, abs=1e-9)
        assert solution.offset.up_m == pytest.approx(0.0, abs=1e-9)

    def test_positive_azimuth_is_clockwise_from_boresight(self) -> None:
        """The sign convention, asserted rather than assumed. Getting it backwards mirrors every
        track about the boresight, which is a plausible-looking picture and completely wrong."""
        solution = solve(range_m=100.0, azimuth_deg=90.0)

        assert solution.offset.east_m == pytest.approx(100.0)
        assert solution.offset.north_m == pytest.approx(0.0, abs=1e-9)

    def test_the_mount_bearing_rotates_the_whole_beam(self) -> None:
        solution = solve(
            range_m=50.0, azimuth_deg=0.0, mount=MountGeometry(boresight_deg=90.0, tilt_deg=0.0)
        )

        assert solution.offset.east_m == pytest.approx(50.0)
        assert solution.offset.north_m == pytest.approx(0.0, abs=1e-9)

    def test_tilt_lifts_the_beam_and_shortens_its_ground_range(self) -> None:
        """The trade the tilt makes, in one assertion: upward coverage is bought by giving up
        ground-level coverage, and the slant range is unchanged while the ground range shrinks."""
        solution = solve(
            range_m=100.0, azimuth_deg=0.0, mount=MountGeometry(boresight_deg=0.0, tilt_deg=30.0)
        )

        assert solution.offset.up_m == pytest.approx(50.0)
        assert solution.offset.north_m == pytest.approx(100.0 * math.cos(math.radians(30.0)))

    def test_mount_height_is_added_to_the_vertical_offset(self) -> None:
        solution = solve(
            range_m=10.0,
            azimuth_deg=0.0,
            mount=MountGeometry(boresight_deg=0.0, tilt_deg=0.0, height_m=4.0),
        )

        assert solution.offset.up_m == pytest.approx(4.0)

    def test_azimuth_sweeps_within_the_tilted_plane_not_the_horizontal_one(self) -> None:
        """The ordering test. Azimuth is measured in the unit's own frame, so it must be applied
        before the mount rotation; applying the mount first would sweep the beam through the
        horizontal plane and put an off-boresight target at the wrong height entirely.

        At 45 degrees of tilt and 90 degrees of azimuth the target lies on the tilt axis itself,
        which is horizontal — so the correct answer is an offset with no vertical component,
        while the wrong ordering would lift it.
        """
        solution = solve(
            range_m=100.0, azimuth_deg=90.0, mount=MountGeometry(boresight_deg=0.0, tilt_deg=45.0)
        )

        assert solution.offset.up_m == pytest.approx(0.0, abs=1e-9)
        assert solution.offset.east_m == pytest.approx(100.0)

    def test_slant_range_is_preserved_whatever_the_mount_does(self) -> None:
        """Rotations do not change lengths. A failure here means the triad has stopped being
        orthonormal, which would quietly corrupt the covariance too."""
        for tilt in (-40.0, 0.0, 15.0, 70.0):
            for azimuth in (-20.0, 0.0, 20.0):
                solution = solve(
                    range_m=63.0,
                    azimuth_deg=azimuth,
                    mount=MountGeometry(boresight_deg=210.0, tilt_deg=tilt),
                )
                length = math.sqrt(
                    solution.offset.east_m**2
                    + solution.offset.north_m**2
                    + solution.offset.up_m**2
                )
                assert length == pytest.approx(63.0)

    def test_a_measured_elevation_is_used_when_the_unit_supplies_one(self) -> None:
        """The interface carries elevation as optional so a replacement unit that measures it
        needs no change here. This is that path."""
        solution = solve(range_m=100.0, azimuth_deg=0.0, elevation_deg=30.0)

        assert solution.offset.up_m == pytest.approx(50.0)

    def test_a_negative_range_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="range_m"):
            solve(range_m=-1.0, azimuth_deg=0.0)


class TestUncertainty:
    def test_an_unmeasured_elevation_becomes_the_beam_spread_as_a_standard_deviation(
        self,
    ) -> None:
        """"Somewhere in the beam, no further information" is a uniform distribution, whose
        standard deviation is its width over sqrt(12). Not a tuning knob dressed as physics."""
        assert unknown_elevation_sigma_deg(30.0) == pytest.approx(30.0 / math.sqrt(12.0))

    def test_the_vertical_term_dominates_for_a_sensor_with_no_elevation_channel(self) -> None:
        """The whole point of reporting a covariance from this unit. A tight vertical variance
        here would tell the tracker an altitude is known when the beam is 30 degrees tall, and
        the filter would believe it."""
        solution = solve(
            range_m=100.0,
            azimuth_deg=0.0,
            uncertainty=BeamUncertainty(
                range_sigma_m=1.0,
                azimuth_sigma_deg=5.0,
                elevation_sigma_deg=unknown_elevation_sigma_deg(30.0),
            ),
        )

        east_var, north_var, up_var = (solution.covariance_enu[i][i] for i in range(3))
        assert up_var > east_var
        assert up_var > north_var
        assert math.sqrt(up_var) > 10.0

    def test_angular_error_grows_with_range_and_range_error_does_not(self) -> None:
        uncertainty = BeamUncertainty(
            range_sigma_m=1.0, azimuth_sigma_deg=5.0, elevation_sigma_deg=0.0
        )
        near = solve(range_m=10.0, azimuth_deg=0.0, uncertainty=uncertainty)
        far = solve(range_m=100.0, azimuth_deg=0.0, uncertainty=uncertainty)

        # Boresight north: north is along-range, east is cross-range.
        assert far.covariance_enu[1][1] == pytest.approx(near.covariance_enu[1][1])
        assert far.covariance_enu[0][0] == pytest.approx(near.covariance_enu[0][0] * 100.0)

    def test_the_covariance_is_symmetric_and_positive_on_the_diagonal(self) -> None:
        """The canonical schema validates both. A return rejected over a rounding difference in
        the fifteenth decimal place would be an absurd way to lose a detection."""
        solution = solve(
            range_m=77.0,
            azimuth_deg=-13.0,
            mount=MountGeometry(boresight_deg=143.0, tilt_deg=22.0),
            uncertainty=BeamUncertainty(
                range_sigma_m=1.5, azimuth_sigma_deg=4.0, elevation_sigma_deg=9.0
            ),
        )

        covariance = solution.covariance_enu
        for i in range(3):
            assert covariance[i][i] > 0.0
            for j in range(3):
                assert covariance[i][j] == covariance[j][i]

    def test_the_covariance_carries_the_beam_orientation_not_just_its_magnitudes(self) -> None:
        """Anisotropy that is not aligned with the ENU axes must show up as off-diagonal terms.
        A diagonal-only covariance would silently discard the direction of the error, which is
        the information that keeps two crossing tracks apart."""
        solution = solve(
            range_m=50.0,
            azimuth_deg=45.0,
            uncertainty=BeamUncertainty(
                range_sigma_m=1.0, azimuth_sigma_deg=8.0, elevation_sigma_deg=0.0
            ),
        )

        assert abs(solution.covariance_enu[0][1]) > 1.0

    def test_zero_range_gives_no_angular_uncertainty(self) -> None:
        """An angular error subtends no metres at zero range. Degenerate, but it must not
        produce a negative or NaN variance."""
        solution = solve(
            range_m=0.0,
            azimuth_deg=0.0,
            uncertainty=BeamUncertainty(
                range_sigma_m=2.0, azimuth_sigma_deg=10.0, elevation_sigma_deg=10.0
            ),
        )

        assert solution.covariance_enu[2][2] == pytest.approx(0.0)


class TestReportedElevation:
    def test_the_reported_elevation_follows_the_mount_tilt(self) -> None:
        """Derived from the mount, never measured — which is exactly why the covariance carries
        the vertical span the unit could not resolve."""
        solution = solve(
            range_m=10.0, azimuth_deg=0.0, mount=MountGeometry(boresight_deg=0.0, tilt_deg=20.0)
        )

        assert solution.elevation_deg == pytest.approx(20.0)

    def test_a_level_mount_reports_a_level_beam(self) -> None:
        assert solve(range_m=10.0, azimuth_deg=15.0).elevation_deg == pytest.approx(0.0, abs=1e-9)
