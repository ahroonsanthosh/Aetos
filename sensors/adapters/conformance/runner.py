"""Driving an adapter through the suite.

The runner's whole job is sequencing: exercise the lifecycle first, on an adapter nobody has
started, then collect detections, then judge what was collected. Nothing here decides what
conformance means — that is `checks.py` and the specification it mirrors.
"""

from __future__ import annotations

from collections.abc import Callable

from backend.common.clock import Clock
from sensors.adapters.capabilities import FloorThresholds, assess_floor
from sensors.adapters.conformance.checks import (
    PlacementContext,
    check_coverage,
    check_declaration,
    check_detections,
    check_lifecycle,
)
from sensors.adapters.conformance.report import ConformanceReport
from sensors.adapters.interface import SensorAdapter
from sensors.schemas.detection import Detection

DEFAULT_CYCLES = 120
"""Polls made while collecting detections.

Enough that a source at the slowest rate the specification admits produces something to judge,
without a run taking long enough that anyone is tempted to skip it."""


def run_conformance(
    adapter: SensorAdapter,
    *,
    thresholds: FloorThresholds,
    clock: Clock,
    cycles: int = DEFAULT_CYCLES,
    pump: Callable[[int], None] | None = None,
    placement: PlacementContext | None = None,
) -> ConformanceReport:
    """Run the suite against one adapter and report what it established.

    `pump` is called before each poll with the cycle index, and is how a caller advances
    whatever drives the adapter — a `SimClock`, a scenario world, a replayed capture. Without it
    the runner simply polls, which is correct for a live source that produces detections on its
    own schedule and which leaves detection-dependent checks reporting NOT_CHECKABLE for a source
    that does not.

    `placement` supplies the sensor's position and frame so coverage can be checked. It is not
    on the adapter interface because placement belongs to the site rather than to the sensor; a
    caller that has both supplies it, and without it the coverage check reports a gap rather
    than being silently skipped.

    The adapter is left **stopped** on return. A conformance run is an inspection, and leaving a
    source running afterwards would make running the suite against a live deployment have a side
    effect on it.
    """
    if cycles <= 0:
        raise ValueError(f"cycles must be positive, got {cycles}")

    capabilities = adapter.capabilities()

    results = list(check_declaration(adapter, capabilities))

    # Before anything else: the lifecycle checks need an adapter that has never been started,
    # and they leave it started, which is what the collection loop below then needs.
    results.extend(check_lifecycle(adapter))

    detections: list[Detection] = []
    try:
        for cycle in range(cycles):
            if pump is not None:
                pump(cycle)
            detections.extend(adapter.get_detections())

        results.extend(check_detections(capabilities, detections, clock))
        results.append(check_coverage(capabilities, detections, placement))
    finally:
        adapter.stop()

    return ConformanceReport(
        sensor_id=adapter.sensor_id,
        capabilities=capabilities,
        thresholds=thresholds,
        assessment=assess_floor(capabilities, thresholds),
        results=tuple(results),
        detections_observed=len(detections),
    )
