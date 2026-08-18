"""Run-mode selection: which sensors feed the pipeline, and what is refused.

Every refusal asserted here corresponds to a state in which the operator would be shown data
that is not what the badge above it claims. That is the only thing this module is about — the
adapters themselves are tested elsewhere, and nothing downstream of ingestion knows a mode
exists at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.common.clock import SimClock
from backend.common.config import RunMode
from backend.main import ModeRefused, build_hardware_adapter, resolve_sources
from backend.zones.site import GeodeticReference, GeoPoint, HardwareLink, SensorPlacement, Site
from sensors.schemas.detection import SensorType

START = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def hardware_placement(sensor_id: str = "radar_01", **overrides: object) -> SensorPlacement:
    link: dict[str, object] = {
        "driver": "uart_fmcw_radar",
        "port": "/dev/ttyUSB0",
        "baud": 115200,
    }
    link.update(overrides)
    return SensorPlacement(
        sensor_id=sensor_id,
        position=GeoPoint(latitude=51.8971, longitude=-8.4697),
        height_m=2.0,
        boresight_deg=90.0,
        hardware=HardwareLink(**link),  # type: ignore[arg-type]
    )


def simulated_placement(sensor_id: str = "sim_01") -> SensorPlacement:
    return SensorPlacement(
        sensor_id=sensor_id,
        registry_key="sim_radar_medium",
        position=GeoPoint(latitude=51.8971, longitude=-8.4697),
    )


def site_with(*placements: SensorPlacement) -> Site:
    return Site(
        site_id="test_site",
        name="Test site",
        reference=GeodeticReference(latitude=51.8971, longitude=-8.4697, altitude_m=5.0),
        protected_radius_m=100.0,
        sensors=list(placements),
    )


# -- site schema -------------------------------------------------------------------------


def test_a_placement_must_be_simulated_or_real_and_not_both() -> None:
    """A placement wearing both hats has no defined source of detections."""
    with pytest.raises(ValueError, match="exactly one"):
        SensorPlacement(
            sensor_id="confused",
            registry_key="sim_radar_medium",
            hardware=HardwareLink(driver="uart_fmcw_radar", port="/dev/ttyUSB0"),
            position=GeoPoint(latitude=0.0, longitude=0.0),
        )


def test_a_placement_with_neither_is_rejected() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        SensorPlacement(sensor_id="empty", position=GeoPoint(latitude=0.0, longitude=0.0))


def test_an_unknown_driver_is_a_configuration_error_not_an_import_error() -> None:
    with pytest.raises(ValueError):
        HardwareLink(driver="lidar_of_the_future", port="/dev/ttyUSB0")  # type: ignore[arg-type]


# -- mode resolution ---------------------------------------------------------------------


def test_simulation_attaches_no_hardware() -> None:
    attach_hardware, scenario = resolve_sources(
        RunMode.SIMULATION, site_with(simulated_placement(), hardware_placement()), "a_scenario"
    )

    assert attach_hardware is False
    assert scenario == "a_scenario"


def test_simulation_without_a_scenario_is_an_idle_system_not_a_refusal() -> None:
    """`--no-scenario` is a legitimate way to start: serve the log, drive nothing."""
    assert resolve_sources(RunMode.SIMULATION, site_with(simulated_placement()), None) == (
        False,
        None,
    )


def test_a_scenario_with_no_simulated_sensors_is_refused() -> None:
    """It would drive nothing, and the empty display would have no visible cause."""
    with pytest.raises(ModeRefused, match="no enabled simulated sensors"):
        resolve_sources(RunMode.SIMULATION, site_with(hardware_placement()), "a_scenario")


def test_live_attaches_hardware_and_runs_no_scenario() -> None:
    attach_hardware, scenario = resolve_sources(
        RunMode.LIVE, site_with(simulated_placement(), hardware_placement()), None
    )

    assert attach_hardware is True
    assert scenario is None, "simulated placements are not driven in live mode"


def test_live_with_no_hardware_is_refused() -> None:
    """Otherwise the interface wears a LIVE badge over an empty sky nothing is watching."""
    with pytest.raises(ModeRefused, match="no enabled sensor with a hardware block"):
        resolve_sources(RunMode.LIVE, site_with(simulated_placement()), None)


def test_live_with_a_scenario_is_refused_by_name() -> None:
    """Synthetic tracks beside real ones is hybrid, and it has to be asked for."""
    with pytest.raises(ModeRefused, match="HYBRID"):
        resolve_sources(RunMode.LIVE, site_with(hardware_placement()), "a_scenario")


def test_a_disabled_hardware_placement_does_not_satisfy_live() -> None:
    disabled = hardware_placement().model_copy(update={"enabled": False})

    with pytest.raises(ModeRefused):
        resolve_sources(RunMode.LIVE, site_with(disabled), None)


def test_hybrid_requires_both_halves_to_be_real() -> None:
    with pytest.raises(ModeRefused, match="run --mode simulation"):
        resolve_sources(RunMode.HYBRID, site_with(simulated_placement()), "a_scenario")

    with pytest.raises(ModeRefused, match="run --mode live"):
        resolve_sources(RunMode.HYBRID, site_with(hardware_placement()), None)


def test_hybrid_attaches_both() -> None:
    assert resolve_sources(
        RunMode.HYBRID, site_with(simulated_placement(), hardware_placement()), "a_scenario"
    ) == (True, "a_scenario")


# -- adapter construction ------------------------------------------------------------------


def test_placement_geometry_reaches_the_adapter(tmp_path: Path) -> None:
    """Boresight, tilt and position come from the site document, unaltered.

    This is the seam where an installer's tape measure meets the tracker's geometry. A
    transcription error here is a silent bearing error on every detection the sensor produces.
    """
    placement = hardware_placement(tilt_deg=15.0, max_range_m=80.0)

    adapter = build_hardware_adapter(SimClock(START), placement, tmp_path, 5.0)

    config = adapter._config  # noqa: SLF001 — asserting the mapping is the point of the test
    assert adapter.sensor_id == "radar_01"
    assert adapter.sensor_type is SensorType.RADAR
    assert config.mount.boresight_deg == 90.0
    assert config.mount.tilt_deg == 15.0
    assert config.mount.altitude_m == 7.0, (
        "site reference altitude plus mast height, in metres above the ellipsoid (FM-004)"
    )
    assert config.gate.max_range_m == 80.0
    assert config.link.port == "/dev/ttyUSB0"


def test_a_live_adapter_is_not_simulated() -> None:
    """The property everything downstream reads to decide whether the badge says LIVE."""
    assert SensorType.RADAR.is_simulated is False
    assert SensorType.SIMULATED_RADAR.is_simulated is True


def test_the_raw_log_path_is_resolved_under_the_data_directory(tmp_path: Path) -> None:
    """Relative in configuration, absolute by the time the driver holds it — so a session's
    raw capture lands beside its event log rather than wherever the process was started."""
    placement = hardware_placement(raw_log_path="raw/radar_01.log")

    adapter = build_hardware_adapter(SimClock(START), placement, tmp_path)

    assert adapter._config.raw_log_path == tmp_path / "raw/radar_01.log"  # noqa: SLF001


def test_construction_touches_no_hardware(tmp_path: Path) -> None:
    """Building the adapter set from configuration must not fail on an unplugged device, and
    must not need pyserial installed to try."""
    adapter = build_hardware_adapter(SimClock(START), hardware_placement(), tmp_path)

    assert adapter.health().state.value == "not_started"
