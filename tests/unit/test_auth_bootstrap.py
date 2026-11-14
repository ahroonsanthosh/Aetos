"""Accounts declared by the environment, for hosted platforms with no shell."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.auth import admin
from backend.auth.bootstrap import (
    MIN_PASSWORD_LENGTH,
    BootstrapRefused,
    bootstrap_accounts,
    declared_accounts,
)
from backend.auth.models import Role
from backend.auth.policy import AuthPolicy
from backend.auth.service import AuthService
from backend.common.clock import SimClock
from backend.database.sqlite import SqliteRepositories
from backend.events.store import SqliteEventStore

PASSWORD = "a-long-enough-passphrase"
OTHER = "a-different-long-passphrase"


@pytest.fixture
def auth():
    clock = SimClock(datetime(2026, 11, 3, 12, 0, tzinfo=UTC))
    repos = SqliteRepositories(clock)
    events = SqliteEventStore(clock)
    service = AuthService(
        AuthPolicy(scrypt_n=2**12, scrypt_r=8, scrypt_p=1),
        clock,
        repos.users,
        repos.credentials,
        repos.sessions,
        events,
    )
    yield service
    repos.close()
    events.close()


def test_nothing_declared_means_nothing_happens(auth: AuthService) -> None:
    assert bootstrap_accounts(auth, {}) == []
    assert auth.user_count() == 0


def test_a_declared_admin_is_created_and_can_sign_in(auth: AuthService) -> None:
    env = {"AETOS_ADMIN_USERNAME": "Alice", "AETOS_ADMIN_PASSWORD": PASSWORD}
    bootstrap_accounts(auth, env)

    result = auth.authenticate("alice", PASSWORD)
    assert result.succeeded
    assert result.user.role == Role.ADMIN


def test_a_declared_viewer_gets_the_viewer_role(auth: AuthService) -> None:
    env = {"AETOS_VIEWER_USERNAME": "guest", "AETOS_VIEWER_PASSWORD": PASSWORD}
    bootstrap_accounts(auth, env)

    assert auth.authenticate("guest", PASSWORD).user.role == Role.VIEWER


def test_a_restart_with_the_same_environment_changes_nothing(auth: AuthService) -> None:
    env = {"AETOS_ADMIN_USERNAME": "alice", "AETOS_ADMIN_PASSWORD": PASSWORD}
    bootstrap_accounts(auth, env)

    assert auth.ensure_account("alice", PASSWORD, Role.ADMIN) == "unchanged"
    assert auth.user_count() == 1


def test_changing_the_declared_password_replaces_the_old_one(auth: AuthService) -> None:
    bootstrap_accounts(auth, {"AETOS_ADMIN_USERNAME": "alice", "AETOS_ADMIN_PASSWORD": PASSWORD})
    bootstrap_accounts(auth, {"AETOS_ADMIN_USERNAME": "alice", "AETOS_ADMIN_PASSWORD": OTHER})

    assert not auth.authenticate("alice", PASSWORD).succeeded
    assert auth.authenticate("alice", OTHER).succeeded


def test_an_existing_account_is_moved_to_the_declared_role(auth: AuthService) -> None:
    auth.create_user("alice", PASSWORD, Role.VIEWER)

    assert auth.ensure_account("alice", PASSWORD, Role.ADMIN) == "updated"
    assert auth.authenticate("alice", PASSWORD).user.role == Role.ADMIN


@pytest.mark.parametrize(
    "env",
    [
        {"AETOS_ADMIN_USERNAME": "alice"},
        {"AETOS_ADMIN_PASSWORD": PASSWORD},
        {"AETOS_VIEWER_USERNAME": "guest", "AETOS_VIEWER_PASSWORD": ""},
    ],
)
def test_a_half_declared_account_refuses_the_start(env: dict[str, str]) -> None:
    with pytest.raises(BootstrapRefused, match="must be set together"):
        declared_accounts(env)


def test_a_short_password_refuses_the_start() -> None:
    env = {"AETOS_ADMIN_USERNAME": "alice", "AETOS_ADMIN_PASSWORD": "short"}
    with pytest.raises(BootstrapRefused, match="at least"):
        declared_accounts(env)


def test_admin_and_viewer_cannot_share_a_name() -> None:
    env = {
        "AETOS_ADMIN_USERNAME": "alice",
        "AETOS_ADMIN_PASSWORD": PASSWORD,
        "AETOS_VIEWER_USERNAME": "ALICE",
        "AETOS_VIEWER_PASSWORD": OTHER,
    }
    with pytest.raises(BootstrapRefused, match="same account"):
        declared_accounts(env)


def test_the_length_rule_matches_the_cli() -> None:
    assert MIN_PASSWORD_LENGTH == admin.MIN_PASSWORD_LENGTH
