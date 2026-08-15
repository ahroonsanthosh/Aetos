"""Account management from the command line.

**Run this with the interpreter inside `.venv`, not the bare `python` on PATH.** The
dependencies are installed into the virtual environment `start.sh` creates, so the system
interpreter almost certainly cannot import pydantic and fails before argparse ever runs:

    .venv/bin/python -m backend.auth.admin ...          # Linux, macOS
    .venv/Scripts/python.exe -m backend.auth.admin ...  # Windows, in Git Bash

Written out because the Windows layout is `Scripts` rather than `bin` and nobody guesses that,
and because the resulting `ModuleNotFoundError` names pydantic — which looks like a broken
install rather than the wrong interpreter.

    ... add --username alice --role admin
    ... list
    ... passwd --username alice
    ... role --username bob --role viewer
    ... disable --username bob
    ... unlock --username alice

This exists because the first account has to come from somewhere, and every other option is
worse. A default credential ships a known password to every deployment. A first-run generated
password has to be printed, which puts it in a terminal scrollback and usually a log. A
self-service signup endpoint on a system with no accounts is an open door.

So: a deliberate act, at a shell, by someone who already has access to the machine. Passwords
are prompted for rather than passed as arguments — an argument lands in shell history and in
the process table, where any other user on the box can read it.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from backend.auth.models import Role
from backend.auth.policy import load_auth_policy
from backend.auth.service import AuthService
from backend.common.clock import SystemClock
from backend.common.config import ConfigPaths
from backend.database.sqlite import SqliteRepositories
from backend.events.store import SqliteEventStore

MIN_PASSWORD_LENGTH = 12
"""Short passwords are the failure this actually sees.

Length only, with no character-class rules. Composition requirements are known to push people
towards `Password1!` and towards writing it down, while length is what actually costs an
attacker anything. Current NIST guidance says the same.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.auth.admin",
        description="Manage Aetos operator accounts.",
    )
    parser.add_argument("--data-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="create an account")
    add.add_argument("--username", required=True)
    add.add_argument("--role", required=True, choices=[str(r) for r in Role])
    add.add_argument("--display-name", default=None)

    sub.add_parser("list", help="list accounts")

    for name, help_text in (
        ("passwd", "set an account's password"),
        ("disable", "disable an account and end its sessions"),
        ("enable", "re-enable an account"),
        ("unlock", "clear a lockout before its window elapses"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--username", required=True)

    role = sub.add_parser("role", help="change an account's role")
    role.add_argument("--username", required=True)
    role.add_argument("--role", required=True, choices=[str(r) for r in Role])

    arguments = parser.parse_args(argv)

    clock = SystemClock()
    # Accounts are installation-wide and live outside the per-deployment data directories, so
    # this resolves the identity database exactly the way the server does — from one function,
    # rather than from two paths that agree until one of them changes. When they disagree the
    # failure is silent and complete: accounts created successfully here, and a login screen
    # that rejects every one of them.
    data_dir = arguments.data_dir or ConfigPaths.default().repo_root / "data" / "admin"
    identity_path = (
        (arguments.data_dir / "identity.db")
        if arguments.data_dir
        else ConfigPaths.default().identity_db
    )
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    repositories = SqliteRepositories(
        clock, data_dir / "admin.db", identity_path=identity_path
    )
    events = SqliteEventStore(clock, data_dir / "events.db")

    auth = AuthService(
        load_auth_policy(),
        clock,
        repositories.users,
        repositories.credentials,
        repositories.sessions,
        events,
    )

    try:
        return _dispatch(arguments, auth, repositories)
    finally:
        repositories.close()
        events.close()


def _dispatch(arguments: argparse.Namespace, auth: AuthService, repos: SqliteRepositories) -> int:
    command = arguments.command

    if command == "list":
        return _list(repos)

    if command == "add":
        password = _prompt_new_password()
        if password is None:
            return 1
        try:
            user = auth.create_user(
                username=arguments.username,
                password=password,
                role=Role(arguments.role),
                display_name=arguments.display_name,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Created {user.username} ({user.role}).")
        return 0

    user = repos.users.by_username(arguments.username.strip().lower())
    if user is None:
        print(f"error: no such account {arguments.username!r}", file=sys.stderr)
        return 1

    if command == "passwd":
        password = _prompt_new_password()
        if password is None:
            return 1
        auth.set_password(user.user_id, password)
        print(f"Password changed for {user.username}.")
        print(
            "Existing sessions were left alone. If this was a reset rather than a routine "
            "change, run 'disable' then 'enable' to end them."
        )
        return 0

    if command == "role":
        auth.set_role(user.user_id, Role(arguments.role))
        print(f"{user.username} is now {arguments.role}.")
        return 0

    if command == "disable":
        auth.set_enabled(user.user_id, False)
        print(f"{user.username} disabled; every session it held has been ended.")
        return 0

    if command == "enable":
        auth.set_enabled(user.user_id, True)
        print(f"{user.username} enabled.")
        return 0

    if command == "unlock":
        auth.clear_lockout(user.user_id)
        print(f"Lockout cleared for {user.username}.")
        return 0

    raise AssertionError(f"unhandled command {command!r}")


def _list(repos: SqliteRepositories) -> int:
    users = repos.users.list_all()
    if not users:
        print(f"No accounts. Create one with:\n  {sys.executable} -m backend.auth.admin add "
              "--username <name> --role admin")
        return 0

    print(f"{'USERNAME':<24} {'ROLE':<10} {'STATE':<10} CREATED")
    for user in users:
        state = "enabled" if user.enabled else "disabled"
        print(
            f"{user.username:<24} {str(user.role):<10} {state:<10} "
            f"{user.created_at.isoformat(timespec='seconds')}"
        )
    return 0


_MSYS_ENV_VAR = "MSYSTEM"
"""Set by Git Bash (MINGW64, MINGW32, MSYS) for every shell it starts. The reliable way to
detect the environment below, rather than reacting to a hang."""


def _read_password(prompt: str) -> str:
    """`getpass.getpass`, except under Git Bash on Windows, where it hangs rather than fails.

    Windows `getpass` reads the console directly through `msvcrt`, bypassing stdin entirely.
    That requires a real Win32 console attached to the process. MinTTY — the terminal Git Bash
    runs in — does not attach one: keystrokes go to a pipe instead, so the `msvcrt` read blocks
    forever on a console that was never there. It looks exactly like a frozen prompt, because it
    is one: nothing is wrong with the terminal, the wrong layer is listening.

    `input()` reads through stdin, which MinTTY does connect properly, so it works — the
    trade is that the password is not hidden while typing. That is worse than hidden entry and
    better than a command that cannot be run at all.
    """
    if sys.platform == "win32" and os.environ.get(_MSYS_ENV_VAR):
        print(f"{prompt}(visible while typing — Git Bash cannot hide input here)")
        return input().strip()
    return getpass.getpass(prompt)


def _prompt_new_password() -> str | None:
    """Prompt twice, without echo where the terminal allows it. Returns None if the two differ
    or it is too short."""
    password = _read_password("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(
            f"error: password must be at least {MIN_PASSWORD_LENGTH} characters",
            file=sys.stderr,
        )
        return None

    if password != _read_password("Repeat: "):
        print("error: passwords do not match", file=sys.stderr)
        return None

    return password


if __name__ == "__main__":
    raise SystemExit(main())
