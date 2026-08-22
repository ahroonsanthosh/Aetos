"""The conformance suite, tested against adapters that are deliberately wrong.

A suite that only ever passes is not a suite. The tests that matter here are the ones that break
one requirement at a time and check the report says so — a conformance mechanism that could not
fail would be exactly the unfalsifiable claim ADR 0017 exists to replace.

The simulated adapter is the positive case. It is held to the same contract as any real one, and
a suite whose only subject was exempt from the rules it enforces would be measuring nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.common.clock import Clock, SimClock
from backend.tracking.requirements import sensor_floor_thresholds
from backend.zones.geometry import load_site_geometry
from scenarios.library import load_all
from scenarios.world import ScenarioWorld
from sensors.adapters.capabilities import SensorCapabilities, SensorFloor
from sensors.adapters.conformance import (
    CheckOutcome,
    PlacementContext,
    run_conformance,
)
from sensors.adapters.interface import (
    SensorAdapter,
    SensorAdapterError,
    SensorHealth,
    SensorState,
)
from sensors.adapters.registry import load_sensor_registry
from sensors.adapters.simulated.adapter import SimulatedSensorAdapter
from sensors.schemas.detection import Detection

EPOCH = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
SCENARIO = "b_approaching_threat"


@pytest.fixture(scope="module")
def geometry():
    return load_site_geometry("fictional_test_range")


@pytest.fixture(scope="module")
def thresholds(geometry):
    return sensor_floor_thresholds(
        pipeline_tick_s=0.5, protected_radius_m=geometry.site.protected_radius_m
    )


def build_simulated(geometry, sensor_id: str = "radar_north"):
    """One simulated adapter driven by a real scenario, with its placement, so coverage can
    actually be checked rather than reported as a gap."""
    scenario = load_all()[SCENARIO]
    clock = SimClock(EPOCH)
    world = ScenarioWorld(scenario, EPOCH)
    registry = load_sensor_registry()
    positions = geometry.site.sensor_positions_enu()
    placement = next(p for p in geometry.site.sensors if p.sensor_id == sensor_id)

    adapter = SimulatedSensorAdapter(
        clock=clock,
        sensor_id=placement.sensor_id,
        model=registry[placement.registry_key],
        position_enu=positions[placement.sensor_id],
        frame=geometry.frame,
        truth_source=world,
        scenario_seed=scenario.seed,
        boresight_deg=placement.boresight_deg,
    )
    context = PlacementContext(
        positions[placement.sensor_id], geometry.frame, placement.boresight_deg
    )
    return adapter, clock, context


# ------------------------------------------------------------------------------------------
# The positive case
# ------------------------------------------------------------------------------------------


def test_the_simulated_radar_is_conformant(geometry, thresholds) -> None:
    adapter, clock, context = build_simulated(geometry)

    report = run_conformance(
        adapter,
        thresholds=thresholds,
        clock=clock,
        pump=lambda _: clock.advance(0.5),
        placement=context,
    )

    assert report.conformant, report.render()
    assert report.failures == ()
    assert report.floor is SensorFloor.FUSABLE
    assert report.detections_observed > 0


def test_the_narrow_field_camera_is_ingestible_but_not_trackable(geometry, thresholds) -> None:
    """A genuinely correct and non-obvious verdict, and the reason the ladder exists.

    The simulated electro-optical profile is precise in bearing and poor in range by an order of
    magnitude — which is what a camera is. It cannot sustain a track alone, and it contributes
    bearing and classification evidence to tracks other sensors are holding. A pass/fail floor
    would either exclude it or admit it with no way to state the limit.
    """
    adapter, clock, context = build_simulated(geometry, sensor_id="eo_ir_east")

    report = run_conformance(
        adapter,
        thresholds=thresholds,
        clock=clock,
        pump=lambda _: clock.advance(0.5),
        placement=context,
    )

    assert report.floor is SensorFloor.INGESTIBLE
    assert report.failures == (), "a limited source is not a non-conforming one"
    assert any("R1.2" in shortfall for shortfall in report.assessment.shortfalls)


def test_every_check_carries_an_explanation(geometry, thresholds) -> None:
    """Including the passes. A check that passes silently invites a reader to assume it
    established more than it did."""
    adapter, clock, context = build_simulated(geometry)

    report = run_conformance(
        adapter, thresholds=thresholds, clock=clock, pump=lambda _: clock.advance(0.5),
        placement=context,
    )

    assert report.results
    assert all(result.detail.strip() for result in report.results)
    assert all(result.title.strip() for result in report.results)


def test_the_report_says_it_only_establishes_declared_conformance(geometry, thresholds) -> None:
    """The over-claim this whole mechanism is built to prevent, arriving one level up. No test
    can determine that a sensor claiming three metres delivers it."""
    adapter, clock, context = build_simulated(geometry)

    rendered = run_conformance(
        adapter, thresholds=thresholds, clock=clock, pump=lambda _: clock.advance(0.5),
        placement=context,
    ).render()

    assert "DECLARED conformance" in rendered
    assert "cannot establish that the claims are" in rendered


def test_the_adapter_is_left_stopped(geometry, thresholds) -> None:
    """A conformance run is an inspection. Leaving a source running would make running the suite
    against a live deployment have a side effect on it."""
    adapter, clock, context = build_simulated(geometry)

    run_conformance(
        adapter, thresholds=thresholds, clock=clock, pump=lambda _: clock.advance(0.5),
        placement=context,
    )

    assert adapter.health().state is SensorState.OFFLINE


# ------------------------------------------------------------------------------------------
# Gaps are not passes
# ------------------------------------------------------------------------------------------


def test_a_run_with_no_detections_reports_gaps_rather_than_passes(
    geometry, thresholds
) -> None:
    """A run that saw nothing cannot establish anything about emitted detections, and reporting
    that as a pass would be the over-claim in miniature.

    A source watching genuinely empty airspace, rather than an adapter that failed: emitting
    nothing is correct behaviour, and the suite's response to it must be "not established"
    rather than either a pass or a failure.
    """
    adapter, clock, context = build_simulated(geometry)

    class Quiet(_Delegating):
        def get_detections(self) -> list[Detection]:
            super().get_detections()
            return []

    report = run_conformance(
        Quiet(adapter), thresholds=thresholds, clock=clock, cycles=4, placement=context
    )

    assert report.detections_observed == 0
    assert report.gaps
    assert any(gap.requirement == "R0.3" for gap in report.gaps)
    assert all(gap.outcome is CheckOutcome.NOT_CHECKABLE for gap in report.gaps)


def test_coverage_is_a_gap_without_a_placement(geometry, thresholds) -> None:
    """Coverage is a statement about geometry, and geometry needs to know where the sensor is.
    Placement belongs to the site, not the sensor, so it is supplied rather than assumed."""
    adapter, clock, _ = build_simulated(geometry)

    report = run_conformance(
        adapter, thresholds=thresholds, clock=clock, pump=lambda _: clock.advance(0.5)
    )

    coverage = next(r for r in report.results if r.requirement == "R0.5")
    assert coverage.outcome is CheckOutcome.NOT_CHECKABLE
    assert "placement" in coverage.detail


def test_unvalidated_coverage_is_reported_as_a_gap(geometry, thresholds) -> None:
    """A datasheet range is quoted against a radar cross-section usually orders of magnitude
    larger than a small multirotor, so declared coverage is an unverified claim until flown."""
    adapter, clock, context = build_simulated(geometry)
    honest = adapter.capabilities()

    class Unvalidated(_Delegating):
        def capabilities(self) -> SensorCapabilities:
            return honest.model_copy(update={"coverage_validated": False})

    report = run_conformance(
        Unvalidated(adapter),
        thresholds=thresholds,
        clock=clock,
        pump=lambda _: clock.advance(0.5),
        placement=context,
    )

    validated = next(r for r in report.results if r.requirement == "D-3")
    assert validated.outcome is CheckOutcome.NOT_CHECKABLE
    assert "FM-016" in validated.detail


# ------------------------------------------------------------------------------------------
# Adapters that are deliberately wrong
# ------------------------------------------------------------------------------------------


def test_an_understated_uncertainty_is_caught(geometry, thresholds) -> None:
    """The single most damaging thing an adapter can do, and the reason the contract is framed
    as honesty rather than accuracy: the filter uses reported uncertainty as its gain, so a
    source claiming to be better than it is will pull tracks off real objects with confidence.
    """
    adapter, clock, context = build_simulated(geometry)
    honest = adapter.capabilities()
    flattering = honest.model_copy(
        update={
            "accuracy": honest.accuracy.model_copy(
                update={"position_uncertainty_m": 0.5, "angular_uncertainty_mrad": 0.0}
            )
        }
    )

    class Optimistic(_Delegating):
        def capabilities(self) -> SensorCapabilities:
            return flattering

    report = run_conformance(
        Optimistic(adapter),
        thresholds=thresholds,
        clock=clock,
        pump=lambda _: clock.advance(0.5),
        placement=context,
    )

    assert not report.conformant
    assert any(failure.requirement == "R0.3" for failure in report.failures)


def test_polling_before_start_returning_quietly_is_caught(geometry, thresholds) -> None:
    """An unstarted adapter reporting no detections is indistinguishable from a working one
    watching an empty sky (FM-010)."""
    adapter, clock, context = build_simulated(geometry)

    class Lenient(_Delegating):
        def get_detections(self) -> list[Detection]:
            if not self._ever_started:
                return []
            return super().get_detections()

    report = run_conformance(
        Lenient(adapter),
        thresholds=thresholds,
        clock=clock,
        pump=lambda _: clock.advance(0.5),
        placement=context,
    )

    lifecycle = [
        r for r in report.results if r.requirement == "R0.7" and "before start" in r.title
    ]
    assert lifecycle and lifecycle[0].outcome is CheckOutcome.FAIL


def test_a_declaration_naming_a_different_sensor_is_caught(geometry, thresholds) -> None:
    adapter, clock, context = build_simulated(geometry)
    honest = adapter.capabilities()

    class Misidentified(_Delegating):
        def capabilities(self) -> SensorCapabilities:
            return honest.model_copy(update={"sensor_id": "somebody_else"})

    report = run_conformance(
        Misidentified(adapter),
        thresholds=thresholds,
        clock=clock,
        pump=lambda _: clock.advance(0.5),
        placement=context,
    )

    assert any(failure.requirement == "D-1" for failure in report.failures)


def test_emitting_outside_declared_coverage_is_caught(geometry, thresholds) -> None:
    """Coverage is what lets silence from a sensor be interpreted. A source reporting outside it
    has declared the wrong coverage, and the operator's read of an empty sector is wrong."""
    adapter, clock, context = build_simulated(geometry)
    honest = adapter.capabilities()
    shrunken = honest.model_copy(
        update={"coverage": honest.coverage.model_copy(update={"max_range_m": 50.0})}
    )

    class Overreaching(_Delegating):
        def capabilities(self) -> SensorCapabilities:
            return shrunken

    report = run_conformance(
        Overreaching(adapter),
        thresholds=thresholds,
        clock=clock,
        pump=lambda _: clock.advance(0.5),
        placement=context,
    )

    assert any(failure.requirement == "R0.5" for failure in report.failures)


def test_an_undeclared_classification_hint_is_caught(geometry, thresholds) -> None:
    """The classification module weights sensor hints as evidence, so an undeclared one enters
    that weighting unaccounted for."""
    adapter, clock, context = build_simulated(geometry, sensor_id="eo_ir_east")
    honest = adapter.capabilities()

    class Quiet(_Delegating):
        def capabilities(self) -> SensorCapabilities:
            return honest.model_copy(update={"classifies": False})

    report = run_conformance(
        Quiet(adapter),
        thresholds=thresholds,
        clock=clock,
        pump=lambda _: clock.advance(0.5),
        placement=context,
    )

    assert any(failure.requirement == "RC.1" for failure in report.failures)


def test_a_run_of_zero_cycles_is_refused(geometry, thresholds) -> None:
    adapter, clock, _ = build_simulated(geometry)

    with pytest.raises(ValueError, match="cycles must be positive"):
        run_conformance(adapter, thresholds=thresholds, clock=clock, cycles=0)


class _Delegating(SensorAdapter):
    """An adapter that forwards everything, so a test can override exactly one thing.

    Subclassing the real adapter would inherit whatever it does; wrapping it means the deviation
    under test is the only difference, which is what makes a failure attributable.
    """

    def __init__(self, inner: SensorAdapter) -> None:
        super().__init__(_ClockOf(inner), inner.sensor_id, inner.sensor_type)
        self._inner = inner
        self._ever_started = False
        self._running = False

    def start(self) -> None:
        self._ever_started = True
        self._running = True
        self._inner.start()

    def stop(self) -> None:
        self._running = False
        self._inner.stop()

    def get_detections(self) -> list[Detection]:
        # The two states are distinct and the contract treats them differently: never started is
        # a wiring error and raises; stopped is a legitimate state a driver may poll through and
        # returns nothing. Conflating them here would make this double fail the very lifecycle
        # check it exists to exercise.
        if not self._ever_started:
            raise SensorAdapterError("polled before start()")
        if not self._running:
            return []
        return self._inner.get_detections()

    def health(self) -> SensorHealth:
        return self._inner.health()

    def capabilities(self) -> SensorCapabilities:
        return self._inner.capabilities()


class _ClockOf(Clock):
    """Reaches the wrapped adapter's clock without the wrapper needing one of its own."""

    def __init__(self, inner: SensorAdapter) -> None:
        self._inner = inner

    def now(self):
        return self._inner._clock.now()

    def monotonic(self) -> float:
        return self._inner._clock.monotonic()
