"""The factory, the registry split, and what each run mode does with the other kind of sensor.

This is the seam that turns "mode selection is adapter registry configuration rather than a code
path" from an intention in `docs/architecture.md` into something that is true. The tests worth
having are about the decisions it makes, not the objects it returns: which placements are built,
which are deliberately skipped, and whether skipping is ever silent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.common.clock import SimClock
from backend.common.config import RunMode
from backend.common.geodesy import EnuPoint
from backend.zones.geometry import load_site_geometry
from scenarios.library import load_all
from scenarios.world import ScenarioWorld
from sensors.adapters.factory import (
    AdapterFactoryError,
    LiveBuildRequest,
    Placement,
    SimulationContext,
    build_adapters,
    driver_registry_reset,
    known_drivers,
    register_driver,
)
from sensors.adapters.registry import (
    LiveSensorModel,
    SensorRegistryError,
    load_sensor_model,
    load_sensor_registry,
)
from sensors.adapters.simulated.sensor_model import SensorModel

EPOCH = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def geometry():
    return load_site_geometry("fictional_test_range")


@pytest.fixture
def registry():
    return load_sensor_registry()


@pytest.fixture
def clock() -> SimClock:
    return SimClock(EPOCH)


@pytest.fixture
def simulation() -> SimulationContext:
    scenario = load_all()["b_approaching_threat"]
    return SimulationContext(
        truth_source=ScenarioWorld(scenario, EPOCH), scenario_seed=scenario.seed
    )


@pytest.fixture(autouse=True)
def isolated_driver_registry():
    """The driver table is process-global, which is right for a plugin registry and wrong for a
    test suite that registers stubs. Snapshot and restore around every test."""
    token = driver_registry_reset()
    yield
    driver_registry_reset(token)


def placements(geometry) -> list[Placement]:
    positions = geometry.site.sensor_positions_enu()
    return [
        Placement(
            sensor_id=p.sensor_id,
            registry_key=p.registry_key,
            position_enu=positions[p.sensor_id],
            boresight_deg=p.boresight_deg,
        )
        for p in geometry.site.sensors
        if p.enabled
    ]


def live_placement() -> Placement:
    return Placement(
        sensor_id="radar_bench",
        registry_key="ld2451_uart",
        position_enu=EnuPoint(0.0, 0.0, 2.0),
    )


# ------------------------------------------------------------------------------------------
# The registry discriminator
# ------------------------------------------------------------------------------------------


def test_the_registry_resolves_both_kinds(registry) -> None:
    assert isinstance(registry["sim_radar_medium"], SensorModel)
    assert isinstance(registry["ld2451_uart"], LiveSensorModel)


def test_an_entry_without_a_kind_is_refused(tmp_path: Path) -> None:
    """No default, because the more permissive kind is the one whose parameters mean nothing on
    real hardware: a live sensor read as simulated would run with a simulator's probability of
    detection quietly applied to it, and nothing would say so."""
    document = json.loads(
        (Path("config/sensor_registry/sim_radar_medium.json")).read_text()
    )
    del document["kind"]
    path = tmp_path / "sim_radar_medium.json"
    path.write_text(json.dumps(document))

    with pytest.raises(SensorRegistryError, match="does not declare a 'kind'"):
        load_sensor_model(path)


def test_an_unknown_kind_names_the_ones_that_exist(tmp_path: Path) -> None:
    path = tmp_path / "odd.json"
    path.write_text(json.dumps({"kind": "telepathic", "registry_key": "odd"}))

    with pytest.raises(SensorRegistryError, match="expected one of"):
        load_sensor_model(path)


def test_a_live_entry_may_not_claim_a_simulated_type(tmp_path: Path) -> None:
    """Simulated-versus-real is carried by `sensor_type` alone, so a real adapter presenting
    itself as simulated would make the interface show real data as synthetic."""
    document = json.loads(Path("config/sensor_registry/ld2451_uart.json").read_text())
    document["sensor_type"] = "simulated_radar"
    path = tmp_path / "ld2451_uart.json"
    path.write_text(json.dumps(document))

    with pytest.raises(SensorRegistryError, match="declared live but names sensor_type"):
        load_sensor_model(path)


def test_a_filename_disagreeing_with_its_key_is_refused(tmp_path: Path) -> None:
    """The entry a reviewer reads must be the entry the system loads."""
    document = json.loads(Path("config/sensor_registry/ld2451_uart.json").read_text())
    path = tmp_path / "something_else.json"
    path.write_text(json.dumps(document))

    with pytest.raises(SensorRegistryError, match="does not match its filename"):
        load_sensor_model(path)


# ------------------------------------------------------------------------------------------
# Run modes
# ------------------------------------------------------------------------------------------


def test_simulation_builds_the_simulated_sensors(geometry, registry, clock, simulation) -> None:
    built = build_adapters(
        placements(geometry),
        registry,
        clock=clock,
        frame=geometry.frame,
        run_mode=RunMode.SIMULATION,
        simulation=simulation,
    )

    assert built.simulated_count == 4
    assert built.live_count == 0
    assert built.skipped == ()


def test_live_mode_skips_simulated_sensors_and_says_why(
    geometry, registry, clock, simulation
) -> None:
    """Skipped, never dropped. A placement configured and not built is the difference between
    the site document and what is running, and the operator must be able to see it."""
    built = build_adapters(
        placements(geometry),
        registry,
        clock=clock,
        frame=geometry.frame,
        run_mode=RunMode.LIVE,
        simulation=simulation,
    )

    assert built.adapters == ()
    assert len(built.skipped) == 4
    assert all(skip.reason for skip in built.skipped)
    assert all("LIVE" in skip.reason for skip in built.skipped)


def test_simulation_mode_skips_live_sensors_and_says_why(registry, clock, geometry) -> None:
    built = build_adapters(
        [live_placement()],
        registry,
        clock=clock,
        frame=geometry.frame,
        run_mode=RunMode.SIMULATION,
    )

    assert built.adapters == ()
    assert len(built.skipped) == 1
    assert "SIMULATION" in built.skipped[0].reason


def test_a_simulated_sensor_with_nothing_to_observe_is_skipped(
    geometry, registry, clock
) -> None:
    """A simulated sensor with no truth source reports a permanently empty sky, which is
    indistinguishable from a working one watching quiet airspace (FM-010)."""
    built = build_adapters(
        placements(geometry),
        registry,
        clock=clock,
        frame=geometry.frame,
        run_mode=RunMode.SIMULATION,
        simulation=None,
    )

    assert built.adapters == ()
    assert all("nothing to observe" in skip.reason for skip in built.skipped)


# ------------------------------------------------------------------------------------------
# Live drivers
# ------------------------------------------------------------------------------------------


def test_no_live_driver_is_registered_by_default() -> None:
    """The honest state of a system with the seam but not the hardware. Asserted so that a
    driver appearing has to be a deliberate act rather than an import side effect nobody
    noticed."""
    assert known_drivers() == ()


def test_a_live_placement_with_no_driver_fails_loudly(registry, clock, geometry) -> None:
    """Raised rather than skipped: an unregistered driver is a configuration error, and a system
    that starts anyway has a coverage gap nobody asked for."""
    with pytest.raises(AdapterFactoryError, match="not registered"):
        build_adapters(
            [live_placement()],
            registry,
            clock=clock,
            frame=geometry.frame,
            run_mode=RunMode.LIVE,
        )


def test_a_registered_driver_is_built_in_live_and_hybrid(
    registry, clock, geometry, simulation
) -> None:
    seen: list[LiveBuildRequest] = []

    def build(request: LiveBuildRequest):
        seen.append(request)
        return _StubAdapter(request)

    register_driver("ld2451_uart", build)

    live = build_adapters(
        [live_placement()],
        registry,
        clock=clock,
        frame=geometry.frame,
        run_mode=RunMode.LIVE,
    )
    assert live.live_count == 1
    assert seen[0].sensor_id == "radar_bench"
    assert seen[0].model.registry_key == "ld2451_uart"

    hybrid = build_adapters(
        [*placements(geometry), live_placement()],
        registry,
        clock=clock,
        frame=geometry.frame,
        run_mode=RunMode.HYBRID,
        simulation=simulation,
    )
    assert hybrid.simulated_count == 4
    assert hybrid.live_count == 1
    assert hybrid.skipped == ()


def test_registering_a_driver_twice_is_refused() -> None:
    """A silently replaced driver means the adapter that runs is not the one the reviewer of the
    registry document believed would run."""
    register_driver("stub", lambda request: _StubAdapter(request))

    with pytest.raises(AdapterFactoryError, match="already registered"):
        register_driver("stub", lambda request: _StubAdapter(request))


def test_a_driver_that_renames_its_sensor_is_refused(registry, clock, geometry) -> None:
    """The event log, health and fusion all key on the identifier."""
    register_driver("ld2451_uart", lambda request: _StubAdapter(request, sensor_id="something"))

    with pytest.raises(AdapterFactoryError, match="identifying itself as"):
        build_adapters(
            [live_placement()],
            registry,
            clock=clock,
            frame=geometry.frame,
            run_mode=RunMode.LIVE,
        )


def test_an_unknown_registry_key_names_the_ones_that_exist(registry, clock, geometry) -> None:
    unknown = Placement(
        sensor_id="ghost", registry_key="no_such_model", position_enu=EnuPoint(0.0, 0.0, 0.0)
    )

    with pytest.raises(AdapterFactoryError, match="Known keys"):
        build_adapters(
            [unknown],
            registry,
            clock=clock,
            frame=geometry.frame,
            run_mode=RunMode.SIMULATION,
        )


class _StubAdapter:
    """The minimum a driver has to hand back for the factory's checks to run.

    Not a `SensorAdapter` subclass on purpose: the factory's contract with a driver is what it
    reads off the returned object, and a test that satisfied the ABC would be asserting the
    ABC rather than the contract.
    """

    def __init__(self, request: LiveBuildRequest, sensor_id: str | None = None) -> None:
        self._request = request
        self.sensor_id = sensor_id or request.sensor_id
        self.sensor_type = request.model.sensor_type

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None
