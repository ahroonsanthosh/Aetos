"""Run the conformance suite against a deployment's configured sensors.

    python -m sensors.adapters.conformance
    python -m sensors.adapters.conformance --site fictional_test_range --sensor radar_north

This is the entry point, and entry points wire things together: it is the one file in this
package that reaches into `backend/` — for the deployment's tracking constants and its site
geometry — so that the suite itself stays free of that dependency and can be run by a third
party against their own adapter with their own thresholds.

Exits non-zero if any sensor is non-conformant, so it can sit in a deployment check.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from backend.common.clock import SimClock
from backend.common.config import RunMode
from backend.tracking.requirements import sensor_floor_thresholds
from backend.zones.geometry import load_site_geometry
from scenarios.library import load_all
from scenarios.world import ScenarioWorld
from sensors.adapters.conformance.checks import PlacementContext
from sensors.adapters.conformance.report import ConformanceReport
from sensors.adapters.conformance.runner import run_conformance
from sensors.adapters.drivers import register_builtin_drivers
from sensors.adapters.factory import Placement, SimulationContext, build_adapters
from sensors.adapters.registry import load_sensor_registry

DEFAULT_SITE = "fictional_test_range"
DEFAULT_SCENARIO = "b_approaching_threat"
DEFAULT_TICK_S = 0.5
DEFAULT_CYCLES = 120


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sensors.adapters.conformance",
        description=(
            "Check a deployment's sensors against the Aetos sensor interface specification. "
            "Reports the floor each source reaches and every requirement it failed."
        ),
    )
    parser.add_argument("--site", default=DEFAULT_SITE, help="site configuration to load")
    parser.add_argument(
        "--scenario",
        default=DEFAULT_SCENARIO,
        help=(
            "scenario to give simulated sensors something to observe. Without one they emit "
            "nothing and every detection-dependent check reports a gap rather than a pass."
        ),
    )
    parser.add_argument("--sensor", default=None, help="check only this sensor_id")
    parser.add_argument(
        "--tick",
        type=float,
        default=DEFAULT_TICK_S,
        help="pipeline cycle interval the thresholds are derived against",
    )
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    arguments = parser.parse_args(argv)

    # A deployment check must be able to build the live sensors it is checking.
    register_builtin_drivers()

    geometry = load_site_geometry(arguments.site)
    thresholds = sensor_floor_thresholds(
        pipeline_tick_s=arguments.tick,
        protected_radius_m=geometry.site.protected_radius_m,
    )

    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    scenario = load_all()[arguments.scenario]
    registry = load_sensor_registry()
    positions = geometry.site.sensor_positions_enu()

    selected = [
        placement
        for placement in geometry.site.sensors
        if placement.enabled
        and (arguments.sensor is None or placement.sensor_id == arguments.sensor)
    ]
    if not selected:
        print(f"No enabled sensor matched {arguments.sensor!r} at site {arguments.site!r}.")
        return 2

    reports: list[ConformanceReport] = []
    for placement in selected:
        # A fresh clock and a fresh world per sensor, and the adapter built against *that*
        # clock. The suite drives one source at a time, so a clock shared across runs would
        # start every sensor after the first one part-way through the scenario — and driving a
        # clock the adapter was not built with advances nothing at all, which shows up as a
        # sensor that mysteriously emitted one detection and then went quiet.
        clock = SimClock(epoch)

        # HYBRID so both kinds are attempted: a live placement with no registered driver fails
        # loudly here, which is the answer a deployment check should give.
        built = build_adapters(
            [
                Placement(
                    sensor_id=placement.sensor_id,
                    registry_key=placement.registry_key,
                    position_enu=positions[placement.sensor_id],
                    boresight_deg=placement.boresight_deg,
                )
            ],
            registry,
            clock=clock,
            frame=geometry.frame,
            run_mode=RunMode.HYBRID,
            simulation=SimulationContext(
                truth_source=ScenarioWorld(scenario, epoch), scenario_seed=scenario.seed
            ),
        )

        for skip in built.skipped:
            print(f"skipped {skip.sensor_id}: {skip.reason}\n")

        for adapter in built.adapters:
            report = run_conformance(
                adapter,
                thresholds=thresholds,
                clock=clock,
                cycles=arguments.cycles,
                pump=lambda _, c=clock: c.advance(arguments.tick),
                placement=PlacementContext(
                    positions[placement.sensor_id], geometry.frame, placement.boresight_deg
                ),
            )
            reports.append(report)
            print(report.render())
            print()

    return _summarise(reports)


def _summarise(reports: list[ConformanceReport]) -> int:
    if not reports:
        print("No adapters were built, so nothing was checked.")
        return 2

    print("=" * 78)
    for report in reports:
        floor = report.floor.label.upper()
        state = "ok  " if report.conformant else "FAIL"
        gaps = f"  ({len(report.gaps)} unchecked)" if report.gaps else ""
        print(f"  {state}  {report.sensor_id:<16} {floor}{gaps}")

    failed = [r for r in reports if not r.conformant]
    print("=" * 78)
    if failed:
        print(f"{len(failed)} of {len(reports)} sensor(s) are not conformant.")
        return 1

    print(f"All {len(reports)} sensor(s) conformant, as declared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
