"""An account created by the admin CLI must be able to sign in to the server.

Obvious, and it broke. Partitioning deployment state per site and run mode (FM-053) moved the
server's database, and the admin CLI kept writing to the old path. `admin add` reported
"Created you (admin)." and the login endpoint answered 401 to the very same credentials.

Nothing failed. Both halves worked exactly as written, against different files.

That is the shape of failure worth a test: not a component that misbehaves, but two components
that each behave correctly while disagreeing about where the truth lives. The check is
end-to-end on purpose — asserting that both call `ConfigPaths.identity_db` would pass even if
the server later stopped using it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.auth import admin
from backend.common.config import ConfigPaths, RunMode
from backend.main import build_application

USERNAME = "rehearsal_operator"
PASSWORD = "a-long-enough-password"


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository root of our own, so the test never touches the real `data/`."""
    repo_root = tmp_path / "install"
    (repo_root / "data").mkdir(parents=True)

    real_default = ConfigPaths.default()
    monkeypatch.setattr(
        ConfigPaths,
        "default",
        classmethod(
            lambda cls: cls(repo_root=repo_root, config_root=real_default.config_root)
        ),
    )
    return repo_root


def create_account(monkeypatch: pytest.MonkeyPatch) -> int:
    # No `raising=False`: if the CLI stops using this helper, the patch must fail loudly
    # rather than silently leaving the real prompt in place for a test to hang on.
    monkeypatch.setattr(admin, "_read_password", lambda *a, **k: PASSWORD)
    return admin.main(["add", "--username", USERNAME, "--role", "admin"])


def test_an_account_created_by_the_cli_can_sign_in(
    installation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact sequence a first-time user follows, and the one that returned 401."""
    assert create_account(monkeypatch) == 0

    app = build_application(
        site="fictional_test_range",
        data_dir=installation / "run",
        mode=RunMode.SIMULATION,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )

    assert response.status_code == 200, (
        "an account created by the admin CLI was rejected by the server — the two are reading "
        "different databases"
    )


def test_the_same_account_signs_in_to_every_run_mode(
    installation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accounts are installation-wide. Track history is partitioned per deployment; identity
    deliberately is not, or an operator would need one account per mode."""
    create_account(monkeypatch)

    for site, mode in (
        ("fictional_test_range", RunMode.SIMULATION),
        ("hardware_testbed", RunMode.LIVE),
    ):
        app = build_application(
            site=site, data_dir=installation / f"run-{mode.value}", mode=mode
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD}
            )
        assert response.status_code == 200, f"sign-in failed for {site} in {mode.value} mode"


def test_accounts_live_outside_the_per_deployment_directories(
    installation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where the file sits is the whole mechanism, so it is asserted rather than assumed."""
    create_account(monkeypatch)

    identity = ConfigPaths.default().identity_db

    assert identity.exists()
    assert identity.parent == installation / "data", (
        "the identity database moved inside a deployment directory; accounts would stop "
        "working the moment someone switched site or run mode"
    )
