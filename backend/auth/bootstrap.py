"""Accounts declared by the environment, for hosts that have no shell.

`backend.auth.admin` is how accounts are made on a machine someone can log in to. A hosted
platform (Render, Fly, Railway and the like) often gives no shell at all, and on a free tier its
disk is wiped on every restart, so an account made by hand would vanish with the next deploy.
There the platform's secret store is the only durable, access-controlled place a credential can
live, and this module reads it:

    AETOS_ADMIN_USERNAME / AETOS_ADMIN_PASSWORD      an admin account
    AETOS_VIEWER_USERNAME / AETOS_VIEWER_PASSWORD    optional read-only account, to share

Every start converges on what the environment says: the account is created if missing, and its
password and role are reset to the declared ones if they differ. Unset means nothing happens,
so a deployment managed with the CLI is unaffected.

This is not a default credential. Nothing is shipped and nothing is generated: the password is
chosen by whoever controls the deployment, in a place only they can read, and the same length
rule as the CLI applies. A half-declared account (a username without a password) refuses the
start rather than being skipped, because a console nobody can sign in to is the state that gets
"fixed" with `--no-auth`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from backend.auth.models import Role
from backend.auth.service import AuthService

_LOG = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 12
"""Matches `backend.auth.admin.MIN_PASSWORD_LENGTH`; a test keeps the two equal."""

DECLARED_ROLES: tuple[tuple[str, Role], ...] = (
    ("AETOS_ADMIN", Role.ADMIN),
    ("AETOS_VIEWER", Role.VIEWER),
)


class BootstrapRefused(RuntimeError):
    """The environment declares an account it is not safe to create."""


@dataclass(frozen=True)
class DeclaredAccount:
    username: str
    password: str
    role: Role


def declared_accounts(environ: Mapping[str, str]) -> list[DeclaredAccount]:
    """The accounts `environ` declares, validated. Raises `BootstrapRefused` on a bad one."""
    accounts: list[DeclaredAccount] = []
    for prefix, role in DECLARED_ROLES:
        username = environ.get(f"{prefix}_USERNAME", "").strip()
        password = environ.get(f"{prefix}_PASSWORD", "")

        if not username and not password:
            continue
        if not username or not password:
            missing = f"{prefix}_PASSWORD" if username else f"{prefix}_USERNAME"
            raise BootstrapRefused(
                f"{prefix}_USERNAME and {prefix}_PASSWORD must be set together; {missing} is "
                "empty. Set both in the hosting platform's environment, or neither."
            )
        if len(password) < MIN_PASSWORD_LENGTH:
            raise BootstrapRefused(
                f"{prefix}_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters. This "
                "console is reachable from the internet; a short password is the way in."
            )
        accounts.append(DeclaredAccount(username=username, password=password, role=role))

    usernames = [a.username.lower() for a in accounts]
    if len(set(usernames)) != len(usernames):
        raise BootstrapRefused(
            "AETOS_ADMIN_USERNAME and AETOS_VIEWER_USERNAME name the same account. Give the "
            "viewer account its own name."
        )
    return accounts


def bootstrap_accounts(auth: AuthService, environ: Mapping[str, str]) -> list[str]:
    """Apply the declared accounts. Returns one log line per account, for the start-up log."""
    reports = []
    for account in declared_accounts(environ):
        outcome = auth.ensure_account(account.username, account.password, account.role)
        line = f"Account {account.username.strip().lower()!r} ({account.role}) {outcome} from env"
        _LOG.info(line)
        reports.append(line)
    return reports
