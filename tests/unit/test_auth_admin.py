"""The Git Bash password-prompt workaround.

Windows `getpass` reads the console directly through `msvcrt`, which requires a real Win32
console. MinTTY — the terminal Git Bash runs in — never attaches one, so that read blocks
forever and looks exactly like a frozen prompt. `_read_password` detects the one environment
where this is known to happen and falls back to reading stdin instead, at the cost of visible
input. Everywhere else must keep using hidden entry unchanged.
"""

from __future__ import annotations

import backend.auth.admin as admin


def test_uses_getpass_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(admin.sys, "platform", "linux")
    monkeypatch.setenv(admin._MSYS_ENV_VAR, "MINGW64")  # noqa: SLF001 - exercising the guard
    monkeypatch.setattr(admin.getpass, "getpass", lambda prompt: "hidden")

    assert admin._read_password("Password: ") == "hidden"  # noqa: SLF001


def test_uses_getpass_on_windows_outside_git_bash(monkeypatch) -> None:
    monkeypatch.setattr(admin.sys, "platform", "win32")
    monkeypatch.delenv(admin._MSYS_ENV_VAR, raising=False)  # noqa: SLF001
    monkeypatch.setattr(admin.getpass, "getpass", lambda prompt: "hidden")

    assert admin._read_password("Password: ") == "hidden"  # noqa: SLF001


def test_falls_back_to_stdin_under_git_bash_on_windows(monkeypatch, capsys) -> None:
    """The one combination that hangs on the real `getpass` path. Must not reach it."""
    monkeypatch.setattr(admin.sys, "platform", "win32")
    monkeypatch.setenv(admin._MSYS_ENV_VAR, "MINGW64")  # noqa: SLF001

    def hangs(_prompt: str) -> str:
        raise AssertionError("getpass.getpass must not be called under Git Bash on Windows")

    monkeypatch.setattr(admin.getpass, "getpass", hangs)
    monkeypatch.setattr("builtins.input", lambda: "typed-visibly")

    assert admin._read_password("Password: ") == "typed-visibly"  # noqa: SLF001
    assert "visible while typing" in capsys.readouterr().out
