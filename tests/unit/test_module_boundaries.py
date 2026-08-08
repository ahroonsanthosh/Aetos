"""Architectural rules enforced as tests rather than trusted as conventions.

Conventions decay under deadline pressure; a red test does not. Each check here corresponds
to a decision record, and a failure names the ADR it violates.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
SENSORS = REPO_ROOT / "sensors"

CLOCK_MODULE = BACKEND / "common" / "clock.py"

FORBIDDEN_TIME_CALLS = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("time", "time"),
    ("time", "monotonic"),
    ("time", "perf_counter"),
}


def _python_sources(*roots: Path) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        files.extend(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(files)


def _imported_modules(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(), filename=str(source))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports resolve within the same package, so they cannot reach a
            # sibling top-level module and are not a boundary concern here.
            if node.level == 0 and node.module:
                modules.add(node.module)
    return modules


def test_common_does_not_import_other_backend_modules() -> None:
    """ADR 0009: `common` holds primitives only.

    The moment it depends on a domain module it becomes a dumping ground and starts creating
    import cycles, so the dependency direction is asserted rather than assumed.
    """
    violations: list[str] = []

    for source in _python_sources(BACKEND / "common"):
        for module in _imported_modules(source):
            if module.startswith("backend.") and not module.startswith("backend.common"):
                violations.append(f"{source.relative_to(REPO_ROOT)} imports {module}")

    assert not violations, (
        "backend/common must not depend on any other backend module (ADR 0009):\n  "
        + "\n  ".join(violations)
    )


def test_no_direct_wall_clock_calls_outside_the_clock_module() -> None:
    """ADR 0003: all time comes from an injected Clock.

    Replay, deterministic tests and simulated clock skew all become impossible once
    `datetime.now()` calls are scattered through the codebase, and none of them can be
    retrofitted cheaply.
    """
    violations: list[str] = []

    for source in _python_sources(BACKEND, SENSORS):
        if source == CLOCK_MODULE:
            continue

        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
                continue
            if (func.value.id, func.attr) in FORBIDDEN_TIME_CALLS:
                violations.append(
                    f"{source.relative_to(REPO_ROOT)}:{node.lineno} "
                    f"calls {func.value.id}.{func.attr}()"
                )

    assert not violations, (
        "time must come from an injected Clock, not from wall-clock calls (ADR 0003):\n  "
        + "\n  ".join(violations)
    )


def test_no_effector_or_weapons_vocabulary_in_the_codebase() -> None:
    """The scope boundary, enforced.

    Aetos stops at a recommendation to a human. This check will not stop a determined
    implementation, but it does mean nobody adds effector control without deleting a test
    that says, in plain language, that they should not.
    """
    forbidden = (
        "effector",
        "jammer",
        "jamming",
        "kinetic_intercept",
        "weapon",
        "engage_target",
        "fire_control",
        "countermeasure",
    )
    allowed_paths = {
        # Documentation states the boundary and necessarily names what is out of scope.
        REPO_ROOT / "tests" / "unit" / "test_module_boundaries.py",
    }

    violations: list[str] = []
    for source in _python_sources(BACKEND, SENSORS):
        if source in allowed_paths:
            continue
        lowered = source.read_text().lower()
        for term in forbidden:
            if term in lowered:
                violations.append(f"{source.relative_to(REPO_ROOT)} contains {term!r}")

    assert not violations, (
        "Aetos ends at a human-facing recommendation. Response and effector integration is a "
        "separate system with its own authorisation and safety architecture:\n  "
        + "\n  ".join(violations)
    )


@pytest.mark.parametrize(
    "vendor_term",
    ["echodyne", "dedrone", "robin radar", "sentrion", "aaronia", "hensoldt"],
)
def test_no_sensor_vendor_names_in_the_codebase(vendor_term: str) -> None:
    """The system must never depend on, or appear to depend on, a particular manufacturer.

    A vendor name appearing anywhere in the source is a signal that vendor-specific
    behaviour has leaked past the adapter boundary, which is where it must stay.
    """
    violations = [
        str(source.relative_to(REPO_ROOT))
        for source in _python_sources(BACKEND, SENSORS)
        if source.name != "test_module_boundaries.py" and vendor_term in source.read_text().lower()
    ]

    assert not violations, f"vendor name {vendor_term!r} appears in: {violations}"
