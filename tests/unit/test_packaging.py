"""Every package the application imports must actually be shipped.

`scenarios` was not. `backend.main` imports it at module level, so the process started only
when the working directory happened to be the repository root — `python -m` puts the current
directory on the path, which quietly supplied the missing package. Run from anywhere else, or
from a real (non-editable) install, and it died on `ModuleNotFoundError: No module named
'scenarios'` before argparse, before logging, before any message this codebase would have
written.

That is the whole class of bug this file exists to prevent: a dependency satisfied by accident
of how the process happened to be launched. The check derives the required set from the imports
themselves, so adding a new top-level package and forgetting to ship it fails here rather than
on someone's machine.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APPLICATION_ROOTS = ("backend", "sensors", "scenarios")


def _top_level_local_imports() -> set[str]:
    """Every first-party top-level package imported anywhere in the application."""
    imported: set[str] = set()
    for root in APPLICATION_ROOTS:
        for source in (REPO_ROOT / root).rglob("*.py"):
            if "__pycache__" in source.parts:
                continue
            tree = ast.parse(source.read_text(), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
    return {name for name in imported if (REPO_ROOT / name).is_dir()}


def _declared_packages() -> set[str]:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    include = document["tool"]["setuptools"]["packages"]["find"]["include"]
    return {pattern.rstrip("*") for pattern in include}


def test_every_imported_package_is_declared_in_the_build() -> None:
    missing = _top_level_local_imports() - _declared_packages()

    assert not missing, (
        "these packages are imported by the application but not shipped by pyproject.toml:\n  "
        + "\n  ".join(sorted(missing))
        + "\n\nThe process would import them only when the working directory happened to "
        "supply them, and fail with a bare ModuleNotFoundError anywhere else."
    )


def test_the_application_imports_from_a_foreign_working_directory() -> None:
    """The end-to-end statement of it, because a declaration can be right and the install
    still wrong. Runs in a subprocess from a directory that is not the repository root."""
    import subprocess
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as elsewhere:
        result = subprocess.run(
            [sys.executable, "-c", "import backend.main"],
            cwd=elsewhere,
            capture_output=True,
            text=True,
        )

    assert result.returncode == 0, (
        "the application could not be imported from outside the repository root:\n"
        + result.stderr
    )
