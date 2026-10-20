"""What `GET /system/health` says about a system nobody has driven yet.

Both cases here are ordinary ways to start, and both used to be reported as faults.

An idle deployment — `--no-scenario`, no hardware — is explicitly supported (`resolve_sources`
returns no adapters and no scenario, and `tests/unit/test_run_modes.py` pins that as "an idle
system, not a refusal"). It has no adapters, so the pipeline task never steps, so the watchdog
never ran, so the health endpoint reported STALLED: a red fault raised over a system that is
merely doing nothing, on a perfectly normal start.

That is the failure mode this module is least allowed to have. A watchdog that raises a fault
on an ordinary startup is one operators mute in week one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import build_application

SCENARIO = "b_approaching_threat"


def health_of(client: TestClient) -> tuple[str | None, dict]:
    body = client.get("/api/v1/system/health").json()
    rows = {c["component"]: c for c in body["components"]}
    return body["posture"], rows["system_health"]


def app_for(scenario: str | None, tmp_path: Path):
    return build_application(scenario=scenario, data_dir=tmp_path, authenticated=False)


def test_an_idle_deployment_is_blind_not_stalled(tmp_path: Path) -> None:
    """Nothing attached is BLIND — true, and the actionable half of the two.

    STALLED claims observations *stopped*, which needs something to have been observing. A
    deployment with no sources never had any, so there is nothing for staleness to be about,
    and the assessment does not age into a fault however long the process runs.
    """
    with TestClient(app_for(None, tmp_path)) as client:
        posture, watchdog = health_of(client)

    assert posture == "blind"
    assert watchdog["state"] == "ok", "the watchdog is running; it is the sky that is empty"
    assert "No sensor adapters are attached" in watchdog["detail"]


def test_a_driven_deployment_reports_its_sensors(tmp_path: Path) -> None:
    """The converse, so the fix above cannot be a blanket suppression of the stall check."""
    with TestClient(app_for(SCENARIO, tmp_path)) as client:
        posture, watchdog = health_of(client)

    assert posture == "nominal"
    assert watchdog["state"] == "ok"
    assert "4 source(s)" in watchdog["detail"]


def test_fusion_is_reported_from_the_wiring(tmp_path: Path) -> None:
    """The fusion row was a NOT_BUILT constant for as long as fusion did not exist. Now that it
    does, the row must come from the pipeline that runs it, not from a newer constant."""
    with TestClient(app_for(SCENARIO, tmp_path)) as client:
        body = client.get("/api/v1/system/health").json()

    fusion = {c["component"]: c for c in body["components"]}["fusion"]
    assert fusion["state"] == "ok"
    assert "reconciled" in fusion["detail"]


def test_the_first_request_is_answered_from_a_real_assessment(tmp_path: Path) -> None:
    """No request may be served before the watchdog has judged something.

    The wiring assesses once during construction for exactly this: a health endpoint answering
    "never ran" to the first caller would be reporting on its own startup order rather than on
    the system, and the first caller is often an operator watching a deployment come up.
    """
    app = app_for(SCENARIO, tmp_path)
    watchdog = app.state.services.watchdog

    assert watchdog.latest is not None, "no assessment existed before the app was served"
    assert watchdog.report().posture.value != "stalled"


@pytest.mark.parametrize("scenario", [None, SCENARIO])
def test_the_posture_is_stated_once_and_only_once(scenario: str | None, tmp_path: Path) -> None:
    """Two representations of one fact are free to disagree — the bug this repo keeps finding.

    The posture belongs on the response. The `system_health` row says whether the watchdog is
    running. Restating the posture in the row would give an operator two places to read it and
    one day two different answers.
    """
    with TestClient(app_for(scenario, tmp_path)) as client:
        body = client.get("/api/v1/system/health").json()

    postures = [c for c in body["components"] if c["component"] == "posture"]
    assert postures == [], "the posture must not also appear as a component row"
    assert body["posture"] is not None
