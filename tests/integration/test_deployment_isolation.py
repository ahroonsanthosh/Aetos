"""Persistent state belongs to one deployment, and cannot be inherited by another.

This exists because of a real failure, found by running the system rather than by reading it.
A simulation run and a live run shared `data/`. The projection survives restarts by design
(ADR 0006, FM-030), so `--mode live` started after a scenario **inherited that scenario's
tracks and served them under a LIVE badge** — `simulated: false`, four synthetic tracks, and
nothing anywhere in the data that could have told the operator.

The badge was not lying: real adapters were attached. That is what makes this the dangerous
kind of bug — every individual component was reporting truthfully, and the picture assembled
from them was false.

Two guarantees are tested here, and they pull against each other, which is why both need
pinning:

1. A different deployment never inherits state. Different site, or different run mode, means a
   different history.
2. The **same** deployment always does. Restart recovery is a requirement, not an accident —
   alerts outlive the process that raised them, and a restart that silently discharged them
   would be its own failure (FM-030, ADR 0015).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.common.config import RunMode
from backend.main import (
    DEPLOYMENT_MARKER,
    StartupRefused,
    claim_data_dir,
    resolve_data_dir,
)

# -- partitioning ---------------------------------------------------------------------------


def test_run_modes_at_one_site_do_not_share_a_data_directory() -> None:
    """The exact case that put synthetic tracks on a live display."""
    simulation = resolve_data_dir("a_site", RunMode.SIMULATION)
    live = resolve_data_dir("a_site", RunMode.LIVE)

    assert simulation != live


def test_sites_do_not_share_a_data_directory() -> None:
    """One log per deployment. Two sites in one log is one site's picture at another's."""
    assert resolve_data_dir("site_a", RunMode.LIVE) != resolve_data_dir("site_b", RunMode.LIVE)


def test_the_same_deployment_resolves_to_the_same_directory() -> None:
    """Restart recovery depends on this. It is a requirement, not a leak."""
    assert resolve_data_dir("a_site", RunMode.LIVE) == resolve_data_dir("a_site", RunMode.LIVE)


def test_an_explicit_data_dir_is_honoured_exactly() -> None:
    """Naming a directory means that directory — the partitioning is a default, not a policy
    imposed on someone who has said where they want their data."""
    chosen = Path("/tmp/somewhere/particular")

    assert resolve_data_dir("a_site", RunMode.LIVE, chosen) == chosen


# -- the claim ------------------------------------------------------------------------------


def test_claiming_an_empty_directory_records_the_deployment(tmp_path: Path) -> None:
    claim_data_dir(tmp_path, "a_site", RunMode.LIVE)

    marker = json.loads((tmp_path / DEPLOYMENT_MARKER).read_text())
    assert marker["site"] == "a_site"
    assert marker["mode"] == "live"


def test_reclaiming_by_the_same_deployment_is_allowed(tmp_path: Path) -> None:
    """A restart must be able to pick its own history back up."""
    claim_data_dir(tmp_path, "a_site", RunMode.LIVE)
    claim_data_dir(tmp_path, "a_site", RunMode.LIVE)

    assert (tmp_path / DEPLOYMENT_MARKER).exists()


def test_a_different_mode_cannot_claim_another_deployments_directory(tmp_path: Path) -> None:
    """Belt and braces behind the partitioning: even an explicit --data-dir cannot mix two
    deployments' history."""
    claim_data_dir(tmp_path, "a_site", RunMode.SIMULATION)

    with pytest.raises(StartupRefused, match="Refusing to start"):
        claim_data_dir(tmp_path, "a_site", RunMode.LIVE)


def test_a_different_site_cannot_claim_another_deployments_directory(tmp_path: Path) -> None:
    claim_data_dir(tmp_path, "site_a", RunMode.LIVE)

    with pytest.raises(StartupRefused, match="site_a"):
        claim_data_dir(tmp_path, "site_b", RunMode.LIVE)


def test_the_refusal_says_what_to_do_about_it(tmp_path: Path) -> None:
    """An operator who cannot act on a refusal will work around it, usually by deleting
    something they wanted."""
    claim_data_dir(tmp_path, "a_site", RunMode.SIMULATION)

    with pytest.raises(StartupRefused) as refusal:
        claim_data_dir(tmp_path, "a_site", RunMode.LIVE)

    message = str(refusal.value)
    assert "--data-dir" in message
    assert "simulation" in message and "live" in message


def test_an_unreadable_marker_refuses_rather_than_guesses(tmp_path: Path) -> None:
    """A marker that cannot be parsed says nothing about who owns the directory, and assuming
    it is ours is the assumption that costs an operator their picture."""
    (tmp_path / DEPLOYMENT_MARKER).write_text("{not json")

    with pytest.raises(StartupRefused, match="could not be read"):
        claim_data_dir(tmp_path, "a_site", RunMode.LIVE)


# -- one writer at a time ---------------------------------------------------------------------


def test_a_second_process_cannot_hold_the_same_deployment(tmp_path: Path) -> None:
    """Two processes writing one event log interleave their records (ADR 0006).

    Different ports do not help: the port stops two servers, not two writers.
    """
    from backend.main import DeploymentLock

    first = DeploymentLock(tmp_path / "deployment.lock")
    second = DeploymentLock(tmp_path / "deployment.lock")

    assert first.acquire() is True
    assert second.acquire() is False, "a second holder was allowed to write the same log"

    first.release()
    assert second.acquire() is True, "the lock was not released when its holder let go"
    second.release()


def test_the_lock_records_the_holding_process(tmp_path: Path) -> None:
    """So that whoever hits the refusal can find what is already running."""
    import os

    from backend.main import DeploymentLock

    lock = DeploymentLock(tmp_path / "deployment.lock")
    lock.acquire()

    assert (tmp_path / "deployment.lock").read_text().strip() == str(os.getpid())
    lock.release()


def test_a_second_application_on_the_same_deployment_is_refused(tmp_path: Path) -> None:
    """End to end: the second `./start.sh` against a running deployment stops with a reason."""
    from backend.main import build_application

    first = build_application(
        site="fictional_test_range",
        data_dir=tmp_path / "run",
        authenticated=False,
        mode=RunMode.SIMULATION,
    )

    with pytest.raises(StartupRefused, match="already running this deployment"):
        build_application(
            site="fictional_test_range",
            data_dir=tmp_path / "run",
            authenticated=False,
            mode=RunMode.SIMULATION,
        )

    assert first.state.services is not None, "the first application must remain usable"


def test_an_unwritable_data_directory_refuses_with_a_reason(tmp_path: Path) -> None:
    """A system that keeps no record of what it showed an operator is worse than one that does
    not start — but it must say which of the two happened."""
    from backend.main import claim_data_dir

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")

    with pytest.raises(StartupRefused, match="Could not create the data directory"):
        claim_data_dir(blocker / "inside", "a_site", RunMode.LIVE)


# -- the whole thing, through build_application ----------------------------------------------


def test_a_live_run_cannot_see_a_simulation_runs_tracks(tmp_path: Path) -> None:
    """The end-to-end statement of the bug, at the level a user would hit it.

    Two applications built one after another, as `./start.sh` then `./start.sh --live` does.
    The second must not be looking at the first's event log.
    """
    from backend.main import build_application

    simulation = build_application(
        site="fictional_test_range",
        scenario="a_benign_transit",
        data_dir=tmp_path / "sim",
        authenticated=False,
        mode=RunMode.SIMULATION,
    )

    live = build_application(
        site="hardware_testbed",
        data_dir=tmp_path / "live",
        authenticated=False,
        mode=RunMode.LIVE,
    )

    assert simulation.state.services.events is not live.state.services.events
    assert (tmp_path / "sim" / DEPLOYMENT_MARKER).exists()
    assert (tmp_path / "live" / DEPLOYMENT_MARKER).exists()

    sim_sensors = {s.sensor_id for s in simulation.state.services.sensors.list_all()}
    live_sensors = {s.sensor_id for s in live.state.services.sensors.list_all()}

    assert "radar_testbed_01" in live_sensors
    assert "radar_testbed_01" not in sim_sensors
    assert not (sim_sensors & live_sensors), (
        "a live run must not list the simulated site's sensors — the operator would be shown "
        "sensors that are not fitted and are not reporting"
    )
