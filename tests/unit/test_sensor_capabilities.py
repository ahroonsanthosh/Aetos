"""Capability declarations and the floors they are measured against.

The floors decide which hardware a customer can connect, so the arithmetic behind them is worth
asserting directly rather than only through the conformance suite that consumes it.

Two properties matter more than the rest and are tested first: that the thresholds are *derived*
from tracking's live constants rather than written down, and that the comparison against a
declaration is one-sided — a source may overstate its error and may never understate it.
"""

from __future__ import annotations

import math

import pytest

from backend.tracking.association.gnn import AssociationConfig
from backend.tracking.requirements import (
    DECISION_SCALE_UNCERTAINTY_FRACTION,
    sensor_floor_thresholds,
)
from backend.tracking.track_manager.manager import TrackManagerConfig
from sensors.adapters.capabilities import (
    AccuracyDeclaration,
    Coverage,
    DimensionsDeclaration,
    SensorCapabilities,
    SensorFloor,
    TimingDeclaration,
    TransportDeclaration,
    TransportIntegrity,
    assess_floor,
)
from sensors.schemas.detection import SensorType

THRESHOLDS = sensor_floor_thresholds(pipeline_tick_s=0.5, protected_radius_m=800.0)


def capabilities(**overrides) -> SensorCapabilities:
    """A declaration that clears every floor, so a test can break exactly one thing."""
    defaults = dict(
        sensor_id="bench",
        sensor_type=SensorType.RADAR,
        description="A conforming source, for tests that need one.",
        coverage=Coverage(
            max_range_m=2000.0,
            min_range_m=5.0,
            azimuth_extent_deg=360.0,
            elevation_min_deg=-5.0,
            elevation_max_deg=40.0,
        ),
        timing=TimingDeclaration(
            update_rate_hz=10.0, max_latency_ms=20.0, timestamp_uncertainty_ms=5.0
        ),
        accuracy=AccuracyDeclaration(
            position_uncertainty_m=6.0,
            reference_range_m=2000.0,
            angular_uncertainty_mrad=0.0,
            anisotropy_ratio=1.5,
            reports_covariance=True,
        ),
        dimensions=DimensionsDeclaration(
            reports_range=True,
            reports_bearing=True,
            reports_elevation=True,
            reports_altitude=True,
        ),
        transport=TransportDeclaration(kind="tcp", integrity=TransportIntegrity.VERIFIED),
    )
    return SensorCapabilities(**{**defaults, **overrides})


# ------------------------------------------------------------------------------------------
# Thresholds are derived, not written down — ADR 0017 section 4
# ------------------------------------------------------------------------------------------


def test_the_update_rate_floor_follows_tentative_retirement_not_the_coast_window() -> None:
    """The single most consequential number here, and the one most likely to be read wrongly.

    A *confirmed* track coasts on `coast_seconds` of elapsed time — six seconds, generous. A
    *tentative* one is retired after `tentative_max_misses` missed cycles, counted in cycles
    rather than seconds. A source slower than that can never accumulate the detections
    confirmation requires, whatever its accuracy and however long it observes.

    Reading the coast window as the update-rate floor would admit a source six times slower than
    the tracker can actually form a track from, and the symptom on the operator's screen would
    be tracks that appear and vanish rather than an obvious configuration error.
    """
    tracking = TrackManagerConfig()
    thresholds = sensor_floor_thresholds(pipeline_tick_s=0.5, protected_radius_m=800.0)

    assert thresholds.update_period_ceiling_s == tracking.tentative_max_misses * 0.5
    assert thresholds.update_period_ceiling_s < tracking.coast_seconds


def test_the_thresholds_move_when_the_tracker_is_retuned() -> None:
    """The property that stops the specification becoming a stale duplicate of the code."""
    slower = sensor_floor_thresholds(pipeline_tick_s=1.0, protected_radius_m=800.0)
    faster = sensor_floor_thresholds(pipeline_tick_s=0.25, protected_radius_m=800.0)

    assert slower.update_period_ceiling_s > faster.update_period_ceiling_s

    patient = sensor_floor_thresholds(
        pipeline_tick_s=0.5,
        protected_radius_m=800.0,
        track_config=TrackManagerConfig(tentative_max_misses=6),
    )
    assert patient.update_period_ceiling_s > THRESHOLDS.update_period_ceiling_s


def test_the_gate_radius_comes_from_the_association_configuration() -> None:
    assert THRESHOLDS.gate_radius_sigmas == pytest.approx(
        math.sqrt(AssociationConfig().gate_threshold)
    )


def test_the_uncertainty_ceiling_scales_with_the_site() -> None:
    """A 200 m compound and an 8 km aerodrome do not tolerate the same positional error, so the
    bound is a fraction of the protected radius rather than a fixed number of metres."""
    small = sensor_floor_thresholds(pipeline_tick_s=0.5, protected_radius_m=200.0)
    large = sensor_floor_thresholds(pipeline_tick_s=0.5, protected_radius_m=8000.0)

    assert small.position_uncertainty_ceiling_m == pytest.approx(
        200.0 * DECISION_SCALE_UNCERTAINTY_FRACTION
    )
    assert large.position_uncertainty_ceiling_m > small.position_uncertainty_ceiling_m


@pytest.mark.parametrize(
    ("tick", "radius"), [(0.0, 800.0), (-1.0, 800.0), (0.5, 0.0), (0.5, -10.0)]
)
def test_nonsense_deployment_facts_are_refused(tick: float, radius: float) -> None:
    with pytest.raises(ValueError):
        sensor_floor_thresholds(pipeline_tick_s=tick, protected_radius_m=radius)


# ------------------------------------------------------------------------------------------
# The floors themselves
# ------------------------------------------------------------------------------------------


def test_a_conforming_declaration_reaches_the_top_floor() -> None:
    assessment = assess_floor(capabilities(), THRESHOLDS)

    assert assessment.floor is SensorFloor.FUSABLE
    assert assessment.shortfalls == ()
    assert assessment.reaches(SensorFloor.TRACKABLE)


def test_a_source_slower_than_the_floor_is_ingestible_but_not_trackable() -> None:
    """Not rejected. A slow source still contributes to a track another sensor is holding, and
    excluding it would throw away hardware that is genuinely useful in a supporting role."""
    slow = capabilities(
        timing=TimingDeclaration(
            update_rate_hz=0.2, max_latency_ms=20.0, timestamp_uncertainty_ms=5.0
        )
    )

    assessment = assess_floor(slow, THRESHOLDS)

    assert assessment.floor is SensorFloor.INGESTIBLE
    assert not assessment.reaches(SensorFloor.TRACKABLE)
    assert any("R1.1" in shortfall for shortfall in assessment.shortfalls)
    assert all(shortfall.strip() for shortfall in assessment.shortfalls), (
        "a missed floor with no explanation cannot be acted on"
    )


def test_a_source_with_poor_timekeeping_is_trackable_but_not_fusable() -> None:
    """Timing error is systematic and becomes position error at target speed, so two sensors
    that disagree about the time place one object in two places (FM-023)."""
    drifting = capabilities(
        timing=TimingDeclaration(
            update_rate_hz=10.0, max_latency_ms=20.0, timestamp_uncertainty_ms=5000.0
        )
    )

    assessment = assess_floor(drifting, THRESHOLDS)

    assert assessment.floor is SensorFloor.TRACKABLE
    assert any("R2.1" in shortfall for shortfall in assessment.shortfalls)


def test_anisotropic_error_without_a_covariance_is_not_fusable() -> None:
    """Fusion weights by covariance. A scalar standing in for a 10:1 ellipse contributes the
    source's worst axis to the axis it was actually best at."""
    flattened = capabilities(
        accuracy=AccuracyDeclaration(
            position_uncertainty_m=6.0,
            reference_range_m=2000.0,
            anisotropy_ratio=10.0,
            reports_covariance=False,
        )
    )

    assessment = assess_floor(flattened, THRESHOLDS)

    assert assessment.floor is SensorFloor.TRACKABLE
    assert any("R2.2" in shortfall for shortfall in assessment.shortfalls)


def test_a_bearing_only_source_is_rejected_rather_than_admitted_low() -> None:
    """A source reporting no range cannot place an object at all, so there is nothing for the
    lowest floor to be about."""
    bearing_only = capabilities(
        dimensions=DimensionsDeclaration(reports_range=False, reports_bearing=True)
    )

    assessment = assess_floor(bearing_only, THRESHOLDS)

    assert assessment.floor is SensorFloor.REJECTED
    assert not assessment.floor.is_admitted
    assert assessment.floor.rung is None


def test_a_link_with_no_integrity_at_all_is_rejected() -> None:
    """A corrupted value indistinguishable from a measurement makes every uncertainty this
    source declares meaningless, and uncertainty is what everything downstream is sized from."""
    assessment = assess_floor(
        capabilities(
            transport=TransportDeclaration(kind="serial", integrity=TransportIntegrity.NONE)
        ),
        THRESHOLDS,
    )

    assert assessment.floor is SensorFloor.REJECTED


def test_rejected_is_falsy_and_every_admitted_floor_is_truthy() -> None:
    """The trap this enum's numbering exists to remove.

    The specification numbers the admitted floors 0, 1 and 2, and the obvious encoding would put
    `INGESTIBLE` at zero — making the lowest *passing* grade falsy, so that `if floor:` silently
    treats an admitted sensor as a rejected one. Offsetting by one puts the only value for which
    falsiness could be correct at zero.
    """
    assert not SensorFloor.REJECTED
    assert SensorFloor.INGESTIBLE and SensorFloor.TRACKABLE and SensorFloor.FUSABLE
    assert SensorFloor.INGESTIBLE.rung == 0
    assert SensorFloor.FUSABLE.rung == 2


# ------------------------------------------------------------------------------------------
# Discrimination range — reported, never gated on
# ------------------------------------------------------------------------------------------


def test_discrimination_range_is_where_two_close_objects_stop_being_separable() -> None:
    """Solved for a distance rather than evaluated at one, because cross-range error grows with
    range and the honest answer is "out to here", not yes or no.

    It is often far shorter than the detection range, and it is the figure a customer is least
    likely to have been told.
    """
    growing = capabilities(
        accuracy=AccuracyDeclaration(
            position_uncertainty_m=math.hypot(1.0, 2000.0 * 0.005),
            reference_range_m=2000.0,
            angular_uncertainty_mrad=5.0,
            anisotropy_ratio=1.5,
            reports_covariance=True,
        )
    )

    assessment = assess_floor(growing, THRESHOLDS)
    ceiling = THRESHOLDS.minimum_separation_m / (2.0 * THRESHOLDS.gate_radius_sigmas)

    assert 0.0 < assessment.discrimination_range_m < 2000.0
    # At the reported range the error is exactly at the ceiling; a little further and it is not.
    assert growing.accuracy.uncertainty_at_m(
        assessment.discrimination_range_m
    ) == pytest.approx(ceiling, rel=1e-6)


def test_a_source_whose_error_floor_exceeds_the_ceiling_discriminates_nowhere() -> None:
    """Zero rather than a small number: the range-independent part alone is already too large,
    so there is no range at which it works, including at the sensor."""
    coarse = capabilities(
        accuracy=AccuracyDeclaration(
            position_uncertainty_m=200.0, reference_range_m=2000.0, reports_covariance=True
        )
    )

    assert assess_floor(coarse, THRESHOLDS).discrimination_range_m == 0.0


def test_discrimination_never_exceeds_the_range_the_sensor_can_see() -> None:
    """Discrimination beyond detection is not a capability."""
    precise = capabilities(
        accuracy=AccuracyDeclaration(
            position_uncertainty_m=0.5, reference_range_m=2000.0, reports_covariance=True
        )
    )

    assessment = assess_floor(precise, THRESHOLDS)
    assert assessment.discrimination_range_m == precise.coverage.max_range_m


def test_uncertainty_is_judged_at_the_sensors_own_range_when_that_is_nearer() -> None:
    """A hundred-metre sensor at an eight-hundred-metre site must not be failed on an error it
    would have at a range it cannot reach. Extrapolating its angular term out to the site radius
    computes an uncertainty for a detection it can never produce."""
    short_range = capabilities(
        coverage=Coverage(
            max_range_m=100.0,
            min_range_m=0.5,
            azimuth_extent_deg=40.0,
            elevation_min_deg=-15.0,
            elevation_max_deg=15.0,
        ),
        accuracy=AccuracyDeclaration(
            position_uncertainty_m=15.5,
            reference_range_m=100.0,
            angular_uncertainty_mrad=155.0,
            anisotropy_ratio=15.5,
            reports_covariance=True,
        ),
        dimensions=DimensionsDeclaration(
            reports_range=True, reports_bearing=True, reports_elevation=False
        ),
    )

    assessment = assess_floor(short_range, THRESHOLDS)

    # Extrapolated to 800 m the declared angular term alone would be 124 m, well over the 80 m
    # ceiling — the sensor would fail on an error it cannot have.
    assert short_range.accuracy.uncertainty_at_m(800.0) > (
        THRESHOLDS.position_uncertainty_ceiling_m
    )
    assert assessment.reaches(SensorFloor.TRACKABLE)


# ------------------------------------------------------------------------------------------
# Declarations that contradict themselves
# ------------------------------------------------------------------------------------------


def test_claiming_altitude_without_an_elevation_channel_is_refused() -> None:
    """A source with no elevation channel cannot measure altitude, and declaring otherwise would
    let an assumed figure be rendered as a measurement (ADR 0019)."""
    with pytest.raises(ValueError, match="cannot measure altitude"):
        DimensionsDeclaration(
            reports_range=True,
            reports_bearing=True,
            reports_elevation=False,
            reports_altitude=True,
        )


def test_a_structural_link_must_declare_its_residual() -> None:
    """The plausibility envelope is the only integrity check such a link has, and how much it
    lets through is the whole question."""
    with pytest.raises(ValueError, match="residual_corruption_probability"):
        TransportDeclaration(kind="serial", integrity=TransportIntegrity.STRUCTURAL)


def test_a_verified_link_must_not_declare_a_residual() -> None:
    """Declaring one implies a structural check that is not what is protecting this transport,
    and a reader would draw the wrong conclusion about where the trust comes from."""
    with pytest.raises(ValueError, match="meaningless"):
        TransportDeclaration(
            kind="tcp",
            integrity=TransportIntegrity.VERIFIED,
            residual_corruption_probability=0.2,
        )


def test_the_range_independent_floor_is_recovered_from_the_declaration() -> None:
    """A declaration states its total error at one range; the floor and the angular term are
    separated from it rather than declared twice, so the two cannot disagree."""
    accuracy = AccuracyDeclaration(
        position_uncertainty_m=math.hypot(10.0, 8000.0 * 0.012),
        reference_range_m=8000.0,
        angular_uncertainty_mrad=12.0,
    )

    assert accuracy.range_independent_floor_m() == pytest.approx(10.0)
    assert accuracy.uncertainty_at_m(8000.0) == pytest.approx(accuracy.position_uncertainty_m)
    assert accuracy.uncertainty_at_m(0.0) == pytest.approx(10.0)


def test_a_range_independent_declaration_is_constant() -> None:
    accuracy = AccuracyDeclaration(position_uncertainty_m=7.0, reference_range_m=1000.0)

    assert accuracy.uncertainty_at_m(0.0) == 7.0
    assert accuracy.uncertainty_at_m(50_000.0) == 7.0
