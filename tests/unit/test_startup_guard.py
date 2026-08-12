"""`guard_bind`: the refusal that keeps an open console off a network.

Each refusal is asserted directly, by message content, rather than just "raises" — a guard
that raises for the wrong reason is as dangerous as one that does not raise at all, since it
would still let the actual unsafe case through the moment the message happened to change.
"""

from __future__ import annotations

import pytest

from backend.main import StartupRefused, guard_bind


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", ""])
def test_loopback_hosts_are_always_allowed(host: str) -> None:
    guard_bind(host, authenticated=False, behind_proxy=False, user_count=0)


def test_refuses_unauthenticated_off_loopback() -> None:
    with pytest.raises(StartupRefused, match="authentication disabled"):
        guard_bind("0.0.0.0", authenticated=False, behind_proxy=True, user_count=1)


def test_refuses_no_accounts_off_loopback() -> None:
    with pytest.raises(StartupRefused, match="no accounts"):
        guard_bind("0.0.0.0", authenticated=True, behind_proxy=True, user_count=0)


def test_refuses_off_loopback_with_no_exposure_assertion() -> None:
    with pytest.raises(StartupRefused, match="--behind-proxy or --published-loopback-only"):
        guard_bind("0.0.0.0", authenticated=True, behind_proxy=False, user_count=1)


def test_behind_proxy_satisfies_the_guard() -> None:
    guard_bind("0.0.0.0", authenticated=True, behind_proxy=True, user_count=1)


def test_published_loopback_only_satisfies_the_guard() -> None:
    guard_bind(
        "0.0.0.0",
        authenticated=True,
        behind_proxy=False,
        user_count=1,
        published_loopback_only=True,
    )


def test_published_loopback_only_does_not_waive_the_authentication_check() -> None:
    """A container still needs real auth and a real account — the flag only replaces the TLS
    assertion, not the other two refusals."""
    with pytest.raises(StartupRefused, match="authentication disabled"):
        guard_bind(
            "0.0.0.0",
            authenticated=False,
            behind_proxy=False,
            user_count=1,
            published_loopback_only=True,
        )


def test_published_loopback_only_does_not_waive_the_account_check() -> None:
    with pytest.raises(StartupRefused, match="no accounts"):
        guard_bind(
            "0.0.0.0",
            authenticated=True,
            behind_proxy=False,
            user_count=0,
            published_loopback_only=True,
        )


def test_refusal_names_the_actual_host() -> None:
    """The message names the offending host, not a placeholder — the difference between a
    refusal an operator can act on and one they have to go read the source to understand."""
    with pytest.raises(StartupRefused, match="0.0.0.0"):
        guard_bind("0.0.0.0", authenticated=False, behind_proxy=False, user_count=0)

    with pytest.raises(StartupRefused, match="203.0.113.9"):
        guard_bind("203.0.113.9", authenticated=False, behind_proxy=False, user_count=0)
