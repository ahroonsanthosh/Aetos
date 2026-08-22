"""Run modes as configuration rather than as code that does not exist.

`docs/architecture.md` has always said mode selection is adapter registry configuration rather
than a code path. Until the factory existed that was an intention: `backend/main.py` constructed
simulated adapters directly and had no path that could construct anything else.

These tests are about the parts that are easy to get wrong once the seam is real — that a LIVE
deployment with nothing real attached refuses to start rather than presenting an empty map as
live airspace, and that a pipeline with adapters is actually polled.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.common.config import RunMode
from backend.main import StartupRefused, build_application, guard_sources
from sensors.adapters.factory import AdapterSet, SkippedPlacement
from sensors.schemas.detection import SensorType

SCENARIO = "b_approaching_threat"


class _Source:
    """The only thing `AdapterSet` reads off an adapter for counting purposes."""

    def __init__(self, sensor_type: SensorType) -> None:
        self.sensor_type = sensor_type


def simulated_set(count: int = 2) -> AdapterSet:
    return AdapterSet(tuple(_Source(SensorType.SIMULATED_RADAR) for _ in range(count)))


def live_set(count: int = 1) -> AdapterSet:
    return AdapterSet(tuple(_Source(SensorType.RADAR) for _ in range(count)))


# ------------------------------------------------------------------------------------------
# The startup guard
# ------------------------------------------------------------------------------------------


def test_live_mode_with_only_simulated_adapters_is_refused() -> None:
    """Every screen would say LIVE over synthetic data.

    The mode banner is the one claim the operator interface makes that an operator cannot check
    for themselves, so it has to be true by construction rather than by configuration
    discipline.
    """
    with pytest.raises(StartupRefused, match="LIVE mode"):
        guard_sources(RunMode.LIVE, simulated_set())


def test_a_pipeline_with_no_adapters_at_all_is_refused() -> None:
    """A display with no input. The operator would see a confident empty map, which reads as
    clear airspace rather than as a system that is not connected to anything (FM-010)."""
    with pytest.raises(StartupRefused, match="no sensor adapters"):
        guard_sources(RunMode.SIMULATION, AdapterSet())


def test_the_refusal_names_why_each_placement_was_skipped() -> None:
    """A refusal an operator cannot act on is a refusal they will work around."""
    skipped = AdapterSet(
        skipped=(
            SkippedPlacement("radar_north", "sim_radar_medium", "no scenario attached"),
        )
    )

    with pytest.raises(StartupRefused, match="no scenario attached"):
        guard_sources(RunMode.SIMULATION, skipped)


def test_live_mode_with_a_real_adapter_starts() -> None:
    guard_sources(RunMode.LIVE, live_set())


def test_hybrid_with_some_placements_skipped_is_allowed() -> None:
    """Deliberately not a refusal. A live-fitted site running a training scenario, or a bench
    test of one sensor, legitimately starts fewer adapters than the site defines — and the
    skipped ones are reported rather than suppressed."""
    partial = AdapterSet(
        adapters=live_set().adapters + simulated_set(1).adapters,
        skipped=(SkippedPlacement("eo_ir_west", "sim_eo_ir_narrow", "disabled"),),
    )

    guard_sources(RunMode.HYBRID, partial)


# ------------------------------------------------------------------------------------------
# The wiring
# ------------------------------------------------------------------------------------------


def test_live_mode_refuses_to_start_with_no_driver_registered(tmp_path: Path) -> None:
    """The honest state of a system with the seam but not the hardware.

    No live driver is registered, so a LIVE run has nothing real to build and must say so rather
    than falling back to the simulator — which is precisely the substitution that would put
    synthetic data behind a LIVE banner.
    """
    with pytest.raises(StartupRefused, match="no sensor adapters"):
        build_application(
            scenario=None,
            data_dir=tmp_path,
            authenticated=False,
            run_mode=RunMode.LIVE,
        )


def test_a_simulation_run_reports_itself_as_simulated(tmp_path: Path) -> None:
    app = build_application(
        scenario=SCENARIO, data_dir=tmp_path, authenticated=False, tick_s=0.1
    )

    with TestClient(app) as client:
        mode = client.get("/api/v1/system/mode").json()

    assert mode["mode"] == "simulation"
    assert mode["real_adapters_connected"] == 0
    assert mode["simulated"] is True


def test_the_sensors_endpoint_distinguishes_configured_from_running(tmp_path: Path) -> None:
    """A configured sensor that was never started must not render the same as one that is
    running: the difference reads as a smaller site rather than as a coverage gap."""
    app = build_application(
        scenario=SCENARIO, data_dir=tmp_path, authenticated=False, tick_s=0.1
    )

    with TestClient(app) as client:
        payload = client.get("/api/v1/sensors").json()

    by_id = {sensor["sensor_id"]: sensor for sensor in payload["sensors"]}

    assert payload["count"] == 5
    assert payload["running_count"] == 4

    # `eo_ir_west` is disabled in the site document, so it never reaches the factory and is
    # absent from the skip list. Reading that absence as "started" is the mistake this covers.
    disabled = by_id["eo_ir_west"]
    assert disabled["started"] is False
    assert disabled["not_started_reason"]

    running = by_id["radar_north"]
    assert running["started"] is True
    assert running["not_started_reason"] is None


def test_an_idle_system_reports_no_sensor_as_running(tmp_path: Path) -> None:
    """`--no-scenario` serves the interface over an empty log with nothing attached.

    The regression this guards: "running" was first derived from the *absence* of a skip
    reason, and an adapter set that was never populated has neither adapters nor reasons — so
    every configured sensor read as running while the pipeline polled nothing. A sensors page
    reporting a fully live site on a system watching nothing is precisely the confident display
    over dead data this project exists to avoid.
    """
    app = build_application(scenario=None, data_dir=tmp_path, authenticated=False)

    with TestClient(app) as client:
        payload = client.get("/api/v1/sensors").json()

    assert payload["count"] == 5
    assert payload["running_count"] == 0
    assert all(sensor["started"] is False for sensor in payload["sensors"])
    assert all(sensor["not_started_reason"] for sensor in payload["sensors"])


def test_sensor_type_is_read_from_the_registry_rather_than_left_null(tmp_path: Path) -> None:
    """It was null because nothing loaded the registry, so a type would have been a guess. The
    pipeline holds the registry now, and the type is what tells the interface whether a source
    is simulated or real."""
    app = build_application(
        scenario=SCENARIO, data_dir=tmp_path, authenticated=False, tick_s=0.1
    )

    with TestClient(app) as client:
        payload = client.get("/api/v1/sensors").json()

    assert all(sensor["sensor_type"] for sensor in payload["sensors"])
    assert all(sensor["simulated"] is True for sensor in payload["sensors"])
