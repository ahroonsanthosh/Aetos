"""Configuration loading: what loads, and what a misconfiguration says when it does not.

The failure messages are as much the contract as the success path. A site or environment that
is wrong must stop the system at startup and name the file and the field that stopped it —
half-loading and behaving strangely three hours into a run is the outcome these tests exist to
prevent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.common.config import (
    CONFIG_ROOT_ENV_VAR,
    ConfigError,
    ConfigPaths,
    EnvironmentConfig,
    QueueConfig,
    RiskHysteresisConfig,
    RunMode,
    available_environments,
    available_sensor_registry_keys,
    event_store_path,
    load_config,
    load_environment,
    sensor_registry_entry_path,
    site_config_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_PATHS = ConfigPaths(repo_root=REPO_ROOT, config_root=REPO_ROOT / "config")
SHIPPED_ENVIRONMENTS = ("sim", "staging", "prod")


def _valid_environment(name: str = "sim") -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "environment": name,
        "description": "Fixture environment.",
        "run_mode": "simulation",
        "site": "fictional_test_range",
        "enabled_sensor_keys": None,
        "event_store_path": "var/fixture/events.sqlite3",
        "queues": {
            "ingestion_capacity": 128,
            "tracking_capacity": 64,
            "broadcast_capacity": 32,
            "shed_high_water": 0.9,
            "shed_resume_water": 0.7,
            "sustained_shed_window_s": 10.0,
            "degraded_mode_shed_fraction": 0.05,
        },
        "risk": {
            "hysteresis": {
                "boundaries": [
                    {"level": "medium", "rise_score": 0.35, "fall_score": 0.25},
                    {"level": "high", "rise_score": 0.6, "fall_score": 0.5},
                ],
                "escalation_dwell_s": 2.0,
                "de_escalation_dwell_s": 15.0,
            }
        },
    }


def _tree(tmp_path: Path, name: str, document: Any) -> ConfigPaths:
    """A throwaway config tree containing one environment document."""
    paths = ConfigPaths(repo_root=tmp_path, config_root=tmp_path / "config")
    paths.environments.mkdir(parents=True)
    text = document if isinstance(document, str) else json.dumps(document)
    (paths.environments / f"{name}.json").write_text(text, encoding="utf-8")
    return paths


@pytest.mark.parametrize("name", SHIPPED_ENVIRONMENTS)
def test_every_shipped_environment_loads(name: str) -> None:
    environment = load_environment(name, REPO_PATHS)

    assert environment.environment == name
    assert environment.run_mode is RunMode.SIMULATION
    assert environment.site == "fictional_test_range"


def test_available_environments_lists_the_shipped_set() -> None:
    assert set(SHIPPED_ENVIRONMENTS) <= set(available_environments(REPO_PATHS))


def test_loading_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paths resolve from the repository root, not from wherever the process was started.

    A backend launched by a service manager has no useful working directory, and configuration
    that loads in development but not in deployment is the worst kind of difference.
    """
    monkeypatch.chdir(tmp_path)

    assert load_environment("sim").environment == "sim"


def test_config_root_is_overridable_by_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _tree(tmp_path, "sim", _valid_environment())
    monkeypatch.setenv(CONFIG_ROOT_ENV_VAR, str(paths.config_root))

    assert load_environment("sim").description == "Fixture environment."


def test_explicit_paths_beat_the_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers that pass paths explicitly must not be at the mercy of the ambient environment,
    or a stray export in a shell changes what a test is asserting about."""
    monkeypatch.setenv(CONFIG_ROOT_ENV_VAR, "/nonexistent/config/tree")

    assert load_environment("sim", REPO_PATHS).environment == "sim"


def test_missing_file_names_the_file(tmp_path: Path) -> None:
    paths = ConfigPaths(repo_root=tmp_path, config_root=tmp_path / "config")

    with pytest.raises(ConfigError) as raised:
        load_config(EnvironmentConfig, paths.environment_file("sim"))

    assert "sim.json" in str(raised.value)
    assert "not found" in str(raised.value)


def test_unknown_environment_lists_the_ones_that_do_exist(tmp_path: Path) -> None:
    paths = _tree(tmp_path, "sim", _valid_environment())

    with pytest.raises(ConfigError) as raised:
        load_environment("staging", paths)

    assert "no such environment 'staging'" in str(raised.value)
    assert "available: sim" in str(raised.value)


def test_invalid_json_names_the_file_and_the_position(tmp_path: Path) -> None:
    paths = _tree(tmp_path, "sim", '{"environment": "sim",}')

    with pytest.raises(ConfigError) as raised:
        load_environment("sim", paths)

    message = str(raised.value)
    assert "sim.json" in message
    assert "invalid JSON" in message
    assert "line 1" in message


def test_top_level_must_be_a_json_object(tmp_path: Path) -> None:
    paths = _tree(tmp_path, "sim", "[1, 2, 3]")

    with pytest.raises(ConfigError, match="expected a JSON object"):
        load_environment("sim", paths)


def test_missing_field_names_the_file_and_the_field(tmp_path: Path) -> None:
    document = _valid_environment()
    del document["run_mode"]
    paths = _tree(tmp_path, "sim", document)

    with pytest.raises(ConfigError) as raised:
        load_environment("sim", paths)

    message = str(raised.value)
    assert "sim.json" in message
    assert "run_mode" in message


def test_a_nested_field_is_named_by_its_full_path(tmp_path: Path) -> None:
    """A message saying only "invalid value" for a document with four levels of nesting sends
    the reader looking through the whole file."""
    document = _valid_environment()
    document["queues"]["ingestion_capacity"] = -1
    paths = _tree(tmp_path, "sim", document)

    with pytest.raises(ConfigError) as raised:
        load_environment("sim", paths)

    assert "queues.ingestion_capacity" in str(raised.value)


def test_wrong_type_names_the_field(tmp_path: Path) -> None:
    document = _valid_environment()
    document["queues"]["tracking_capacity"] = "plenty"
    paths = _tree(tmp_path, "sim", document)

    with pytest.raises(ConfigError, match="queues.tracking_capacity"):
        load_environment("sim", paths)


def test_unknown_field_is_rejected_rather_than_ignored(tmp_path: Path) -> None:
    """A typo that is silently ignored is a setting the operator believes is in force and is
    not."""
    document = _valid_environment()
    document["run_moed"] = "simulation"
    paths = _tree(tmp_path, "sim", document)

    with pytest.raises(ConfigError, match="run_moed"):
        load_environment("sim", paths)


def test_environment_name_must_match_the_file_name(tmp_path: Path) -> None:
    """A file copied, renamed and not edited is otherwise indistinguishable from the
    environment it claims to be — which is how simulation tuning ends up in a staging run."""
    paths = _tree(tmp_path, "staging", _valid_environment("sim"))

    with pytest.raises(ConfigError) as raised:
        load_environment("staging", paths)

    assert "declares 'sim'" in str(raised.value)
    assert "loaded as 'staging'" in str(raised.value)


@pytest.mark.parametrize("name", ["../../etc/passwd", "/etc/passwd", "sim/../prod", "Sim"])
def test_a_document_name_may_not_escape_the_config_tree(name: str) -> None:
    with pytest.raises(ConfigError, match="not a valid environment name"):
        load_environment(name, REPO_PATHS)


def test_shed_resume_water_must_be_below_high_water() -> None:
    """Equal trip points give no hysteresis, and the shed policy then oscillates at exactly the
    load where oscillation is most expensive (ADR 0008)."""
    queues = _valid_environment()["queues"]
    queues["shed_resume_water"] = queues["shed_high_water"]

    with pytest.raises(ValueError, match="must be below shed_high_water"):
        QueueConfig.model_validate(queues)


def test_fall_score_must_be_below_rise_score() -> None:
    hysteresis = _valid_environment()["risk"]["hysteresis"]
    hysteresis["boundaries"][0]["fall_score"] = 0.35

    with pytest.raises(ValueError, match="must be below rise_score"):
        RiskHysteresisConfig.model_validate(hysteresis)


def test_boundaries_must_be_ordered_lowest_first() -> None:
    hysteresis = _valid_environment()["risk"]["hysteresis"]
    hysteresis["boundaries"].reverse()

    with pytest.raises(ValueError, match="strictly increasing rise_score"):
        RiskHysteresisConfig.model_validate(hysteresis)


def test_escalation_dwell_may_not_exceed_de_escalation_dwell() -> None:
    """ADR 0007: a genuine threat must not be held back by a de-escalation timer."""
    hysteresis = _valid_environment()["risk"]["hysteresis"]
    hysteresis["escalation_dwell_s"] = 60.0

    with pytest.raises(ValueError, match="must not exceed"):
        RiskHysteresisConfig.model_validate(hysteresis)


def test_environment_resolves_the_site_document_it_names() -> None:
    environment = load_environment("sim", REPO_PATHS)

    assert site_config_path(environment, REPO_PATHS) == (
        REPO_ROOT / "config" / "sites" / "fictional_test_range.json"
    )


def test_a_registry_key_resolves_to_its_own_document() -> None:
    """The registry is one document per sensor model, keyed by file name."""
    path = sensor_registry_entry_path("sim_radar_medium", REPO_PATHS)

    assert path == REPO_ROOT / "config" / "sensor_registry" / "sim_radar_medium.json"
    assert path.is_file()
    assert "sim_radar_medium" in available_sensor_registry_keys(REPO_PATHS)


def test_event_store_path_resolves_against_the_repository_root() -> None:
    environment = load_environment("sim", REPO_PATHS)

    assert event_store_path(environment, REPO_PATHS) == REPO_ROOT / "var/sim/events.sqlite3"


def test_an_absolute_event_store_path_is_left_alone(tmp_path: Path) -> None:
    document = _valid_environment()
    document["event_store_path"] = str(tmp_path / "events.sqlite3")
    paths = _tree(tmp_path, "sim", document)

    environment = load_environment("sim", paths)

    assert event_store_path(environment, paths) == tmp_path / "events.sqlite3"


def test_no_event_store_path_means_an_in_memory_log(tmp_path: Path) -> None:
    document = _valid_environment()
    document["event_store_path"] = None
    paths = _tree(tmp_path, "sim", document)

    assert event_store_path(load_environment("sim", paths), paths) is None


def test_enabled_sensor_keys_restricts_the_registry() -> None:
    """An environment narrows what the registry offers; it never extends it."""
    unrestricted = load_environment("sim", REPO_PATHS)
    restricted = load_environment("staging", REPO_PATHS)

    assert unrestricted.enabled_sensor_keys is None
    assert unrestricted.permits_registry_key("anything_the_site_references")

    assert restricted.permits_registry_key("sim_radar_medium")
    assert not restricted.permits_registry_key("sim_acoustic_array")


def test_live_and_hybrid_run_modes_are_accepted_by_the_schema(tmp_path: Path) -> None:
    """Unimplemented, but not blocked: mode selection is adapter registry configuration rather
    than a code path, so the config shape must not have to change when real adapters exist."""
    document = _valid_environment()
    document["run_mode"] = "hybrid"
    paths = _tree(tmp_path, "sim", document)

    assert load_environment("sim", paths).run_mode is RunMode.HYBRID
