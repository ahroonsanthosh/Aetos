"""Every configuration document the software loads must survive a fresh clone.

This exists because it did not. `config/sites/hardware_testbed.json` — the site `--live` starts
against — was first written as `local_testbed.json`, which matches the `config/**/local_*.json`
rule that keeps personal configuration out of the repository. `git add -A` skipped it without a
word, every test passed because the file was sitting on the author's disk, and the first person
to clone the branch got a stack trace instead of a running system.

The whole class of bug is invisible to ordinary tests: they run in a working tree, and a file
that is present but untracked behaves identically to a file that is committed. Only asking git
tells them apart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "config"


def _git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _in_a_git_checkout() -> bool:
    return (REPO_ROOT / ".git").exists() and _git("rev-parse", "--git-dir").returncode == 0


needs_git = pytest.mark.skipif(
    not _in_a_git_checkout(), reason="not a git checkout — nothing to ask about tracking"
)


def _shipped_config_files() -> list[Path]:
    """Every JSON document under `config/`, which is entirely shipped configuration.

    Anything genuinely local belongs to a name the ignore rules already cover, and by
    definition will not be present in a checkout this test runs against.
    """
    return sorted(p for p in CONFIG.rglob("*.json") if ".git" not in p.parts)


@needs_git
def test_every_shipped_config_document_is_tracked() -> None:
    """A config file present on disk but absent from the repository is a working system for
    exactly one person."""
    assert _shipped_config_files(), "expected shipped configuration under config/"

    tracked = set(_git("ls-files", "config").stdout.split())
    untracked = [
        str(p.relative_to(REPO_ROOT).as_posix())
        for p in _shipped_config_files()
        if str(p.relative_to(REPO_ROOT).as_posix()) not in tracked
    ]

    assert not untracked, (
        "shipped configuration is not committed — a fresh clone would fail to load it:\n  "
        + "\n  ".join(untracked)
        + "\n\nIf the name matches an ignore rule (config/**/local_*.json), rename the file. "
        "Those rules exist to keep personal configuration out of the repository, and a shipped "
        "document must not be wearing a personal name."
    )


@needs_git
def test_the_site_live_mode_defaults_to_is_committed() -> None:
    """Named specifically, because it is the one `./start.sh --live` fails on."""
    site = CONFIG / "sites" / "hardware_testbed.json"

    assert site.exists(), "the default live-mode site is missing from the working tree"
    assert _git("ls-files", "--error-unmatch", str(site)).returncode == 0, (
        "config/sites/hardware_testbed.json is not tracked by git; ./start.sh --live would "
        "fail on a fresh clone"
    )


@needs_git
def test_local_configuration_names_remain_ignored() -> None:
    """The converse. The ignore rule is doing a real job and must not be loosened to fix the
    problem above — a personal site document must still be impossible to commit by accident."""
    result = _git("check-ignore", "config/sites/local_mine.json")

    assert result.returncode == 0, (
        "config/**/local_*.json is no longer ignored; personal site configuration could now be "
        "committed by accident"
    )
