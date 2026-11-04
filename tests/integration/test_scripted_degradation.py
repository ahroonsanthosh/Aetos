"""A scenario's scripted sensor faults reach the sensors.

`sensor_availability` windows were parsed, validated and then never applied, so scenario E's
radar stayed healthy for the whole run and the degraded-sensor demo showed nothing. They also
named sensors (`SIM-RADAR-01`) the shipped site does not have, so fixing the wiring alone would
still have matched nothing. Both are pinned here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.common.clock import SimClock
from backend.main import DEFAULT_SITE, Pipeline
from backend.zones.geometry import load_site_geometry
from scenarios.library import load_all
from sensors.adapters.interface import SensorState

START = datetime(2026, 1, 1, tzinfo=UTC)
TICK_S = 0.5


def _state_at(pipeline: Pipeline, clock: SimClock, until_s: float, sensor_id: str) -> SensorState:
    while (clock.now() - START).total_seconds() < until_s:
        clock.advance(TICK_S)
        pipeline.step()
    (health,) = [h for h in pipeline.sensor_health() if h.sensor_id == sensor_id]
    return health.state


def test_scenario_e_degrades_then_silences_the_north_radar(tmp_path: Path) -> None:
    clock = SimClock(START)
    pipeline = Pipeline(clock, load_site_geometry(DEFAULT_SITE), tmp_path)
    try:
        pipeline.attach_scenario("e_sensor_degradation")
        assert _state_at(pipeline, clock, 50, "radar_north") is SensorState.OK
        assert _state_at(pipeline, clock, 150, "radar_north") is SensorState.DEGRADED
        assert _state_at(pipeline, clock, 250, "radar_north") is SensorState.OFFLINE
        # The fault is the radar's alone.
        assert _state_at(pipeline, clock, 251, "radar_south") is SensorState.OK
    finally:
        pipeline.close()


@pytest.mark.parametrize("scenario_id", sorted(load_all()))
def test_every_scripted_window_names_a_sensor_the_site_has(scenario_id: str) -> None:
    site = load_site_geometry(DEFAULT_SITE).site
    sensors = {placement.sensor_id for placement in site.sensors}
    scripted = {w.sensor_id for w in load_all()[scenario_id].sensor_availability}
    assert scripted <= sensors, f"{scenario_id} scripts {sorted(scripted - sensors)}"


def test_a_scenario_naming_an_unknown_sensor_is_refused(tmp_path: Path, monkeypatch) -> None:
    scenario = load_all()["e_sensor_degradation"]
    window = scenario.sensor_availability[0].model_copy(update={"sensor_id": "no_such_sensor"})
    broken = scenario.model_copy(update={"sensor_availability": [window]})
    monkeypatch.setattr("backend.main.load_all", lambda: {"e_sensor_degradation": broken})

    pipeline = Pipeline(SimClock(START), load_site_geometry(DEFAULT_SITE), tmp_path)
    try:
        with pytest.raises(ValueError, match="no_such_sensor"):
            pipeline.attach_scenario("e_sensor_degradation")
    finally:
        pipeline.close()
