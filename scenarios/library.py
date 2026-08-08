"""Loading the scenario library from disk.

Strict on purpose, in the same way the sensor registry is. A scenario that half-loads, or whose
identifier disagrees with its filename, produces a run that is not the run anybody reviewed —
and since these files double as acceptance criteria, that failure would be silent.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from scenarios.schema import Scenario

SCENARIO_ROOT = Path(__file__).resolve().parent


class ScenarioError(RuntimeError):
    """A scenario file could not be loaded or does not describe what it claims to."""


def scenario_files(root: Path | str | None = None) -> list[Path]:
    """Every scenario document in the library, in a stable order."""
    directory = Path(root) if root is not None else SCENARIO_ROOT
    return sorted(directory.rglob("*.json"))


def load_scenario(file: Path | str) -> Scenario:
    """One scenario. The filename must match the identifier it declares."""
    file = Path(file)
    try:
        document = json.loads(file.read_text())
    except OSError as exc:
        raise ScenarioError(f"could not read scenario {file}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"scenario {file} is not valid JSON: {exc}") from exc

    try:
        scenario = Scenario.model_validate(document)
    except ValidationError as exc:
        raise ScenarioError(f"scenario {file} is invalid:\n{exc}") from exc

    if scenario.scenario_id != file.stem:
        raise ScenarioError(
            f"scenario {file} declares scenario_id {scenario.scenario_id!r}, which does not "
            "match its filename"
        )
    return scenario


def load_all(root: Path | str | None = None) -> dict[str, Scenario]:
    """The whole library, keyed by `scenario_id`."""
    scenarios: dict[str, Scenario] = {}
    for file in scenario_files(root):
        scenario = load_scenario(file)
        if scenario.scenario_id in scenarios:
            raise ScenarioError(f"duplicate scenario_id {scenario.scenario_id!r}")
        scenarios[scenario.scenario_id] = scenario
    return scenarios


def iter_scenarios(root: Path | str | None = None) -> Iterator[tuple[Path, Scenario]]:
    """Each scenario with the path it came from, for tests that report which file failed."""
    for file in scenario_files(root):
        yield file, load_scenario(file)
