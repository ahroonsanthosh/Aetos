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


DRIVER_PACKAGES = SENSORS / "adapters"
"""Where a driver package may live, and the only place a model or manufacturer name may appear.

A driver's whole job is to know about one piece of hardware, so a blanket ban on naming it was
never enforceable once a real one existed — the first live adapter would have had to either
violate the rule or disguise what it talks to, and a driver whose name does not say what it
drives is worse for a reader than one that does.

The property actually worth enforcing is **containment**, which is stronger and is what the
architecture always meant: vendor knowledge may exist in exactly one directory per device, and
nowhere else in `backend/` or `sensors/`. That is checkable, and the check below is what stops
`backend/tracking` acquiring a special case for a particular radar.
"""

VENDOR_TERMS = [
    # Third-party systems the architecture must never appear to depend on. None of these is
    # integrated; they are here so that adding one cannot happen quietly.
    "echodyne",
    "dedrone",
    "robin radar",
    "sentrion",
    "aaronia",
    "hensoldt",
    # Hardware Aetos does drive. Permitted inside its own driver package and nowhere else.
    "hi-link",
    "hilink",
    "ld2451",
    "ld2450",
    "paradisetronic",
    "cp2102",
]


DRIVER_TABLE = SENSORS / "adapters" / "drivers.py"
"""The one file outside a driver package that may name one.

Its entire job is to say which drivers this build can construct, which cannot be done without
naming them. Narrow on purpose: it names them as *identifiers to register*, never as behaviour,
and it is one file rather than a pattern — the moment a second file needs this exemption,
something has leaked.
"""


def _driver_package_of(source: Path) -> Path | None:
    """The driver package a file belongs to, or None if it is not inside one.

    A driver package is an immediate child directory of `sensors/adapters/`. Nesting deeper is
    still contained, so the whole subtree of a driver counts as that driver.
    """
    try:
        relative = source.relative_to(DRIVER_PACKAGES)
    except ValueError:
        return None
    return DRIVER_PACKAGES / relative.parts[0] if len(relative.parts) > 1 else None


@pytest.mark.parametrize("vendor_term", VENDOR_TERMS)
def test_vendor_names_are_confined_to_their_own_driver_package(vendor_term: str) -> None:
    """The system must never depend on, or appear to depend on, a particular manufacturer.

    A vendor or model name outside a driver package is a signal that hardware-specific
    behaviour has leaked past the adapter boundary, which is where it must stay. Inside one it
    is simply what a driver is.

    The failure this is aimed at is not a driver that names its hardware. It is `backend/risk`
    acquiring a threshold that only makes sense for one radar, or `backend/api` branching on a
    model number — each of which starts as one line and is invisible in review.
    """
    violations: list[str] = []

    for source in _python_sources(BACKEND, SENSORS):
        if source.name == "test_module_boundaries.py" or source == DRIVER_TABLE:
            continue
        if vendor_term not in source.read_text().lower():
            continue

        if _driver_package_of(source) is not None:
            # Inside a driver package, which is what containment means. Not restricted to the
            # term matching the package's own name: a driver legitimately names the parts around
            # it too — the USB bridge it is reached through, a companion module — and those are
            # contained by the same directory boundary. What the architecture requires is that
            # none of it is visible from `backend/` or the shared layers of `sensors/`, which is
            # what everything outside this branch checks.
            continue

        violations.append(str(source.relative_to(REPO_ROOT)))

    assert not violations, (
        f"vendor or model name {vendor_term!r} appears outside its own driver package, in: "
        f"{violations}. Hardware knowledge belongs in one directory under sensors/adapters/ and "
        "nowhere else — see docs/sensor_interface_specification.md section 10."
    )
