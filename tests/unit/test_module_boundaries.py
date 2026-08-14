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


RADAR_ADAPTERS = SENSORS / "adapters" / "radar"
CAMERA_ADAPTERS = SENSORS / "adapters" / "camera"


def test_hardware_specifics_stay_inside_their_own_adapter_package() -> None:
    """The separation the swap depends on, asserted rather than trusted.

    A physical sensor is replaceable only for as long as nothing outside its own package knows
    anything about it. The moment a model number, a transport or an imaging library name
    appears elsewhere, swapping the unit stops being a one-file change and nobody finds out
    until the replacement arrives.

    Terms are matched against source text, so the docstring that explains a term counts as a
    reference — deliberately. A module that needs to explain a UART is a module that knows
    about one.
    """
    confined = {
        RADAR_ADAPTERS: ("ld2451", "uart", "pyserial", "baud"),
        CAMERA_ADAPTERS: ("cv2", "opencv", "ultralytics", "yolo", "videocapture"),
    }
    allowed_elsewhere = {
        # This file names what it forbids, and the tests exercise the adapters directly.
        REPO_ROOT / "tests" / "unit" / "test_module_boundaries.py",
        SENSORS / "tests" / "test_camera_adapter.py",
        SENSORS / "tests" / "test_motion_detector.py",
        # A manual bench script for the camera adapter necessarily names its concrete
        # frame source to wrap it for on-the-wire diagnostics; it is not collected by pytest
        # and is run by hand against real hardware, same rationale as the two tests above.
        SENSORS / "tests" / "manual_camera_test.py",
        # Drives a real cv2.VideoCapture on purpose: the frame-freshness behaviour it guards
        # cannot be tested through a mock, since a mock only ever confirms the behaviour it
        # was written to expect — which is exactly how the original lag bug survived review.
        SENSORS / "tests" / "test_video_capture_frame_source.py",
        # Tests the radar's wire format, which cannot be discussed without naming it. Lives
        # outside adapters/radar only because every sensor test lives in sensors/tests.
        SENSORS / "tests" / "test_ld2451_protocol.py",
        # The composition point. It constructs the concrete adapters and documents the swap,
        # so it necessarily names them; that is its entire purpose and the reason the rest of
        # the codebase does not have to. See test_only_the_composition_point_names_a_concrete
        # _adapter, which asserts the other half of this: that nothing else does.
        SENSORS / "live.py",
    }

    # Narrow, per-term exemptions. A whole-file exemption would let a model name follow a
    # generic one into the same file, which is exactly the leak this rule exists to catch.
    generic_vocabulary = {
        # The config schema names what a deployment configures, and a serial device's baud
        # rate is a deployment fact that has to be nameable. Generic transport vocabulary
        # rather than model-specific knowledge: any serial sensor needs it, so it does not
        # make a swap larger. A model name in this file would still fail.
        SENSORS / "config" / "loader.py": {"baud"},
    }

    violations: list[str] = []
    for owner, terms in confined.items():
        for source in _python_sources(BACKEND, SENSORS):
            if owner in source.parents or source in allowed_elsewhere:
                continue
            permitted = generic_vocabulary.get(source, frozenset())
            lowered = source.read_text().lower()
            for term in terms:
                if term in permitted:
                    continue
                if term in lowered:
                    violations.append(
                        f"{source.relative_to(REPO_ROOT)} contains {term!r}, "
                        f"which belongs to {owner.relative_to(REPO_ROOT)}"
                    )

    assert not violations, (
        "sensor hardware specifics must not leak past their own adapter package:\n  "
        + "\n  ".join(violations)
    )


def test_nothing_outside_its_package_imports_the_stub_radar() -> None:
    """The stub is the one file deleted when the physical radar arrives.

    Everything downstream reads `SensorAdapter`, so the only place that may name the stub is
    `sensors/live.py`, the composition point that decides which adapter to construct. Anywhere
    else, the import would have to be found and changed during the swap — which is precisely
    the cost this arrangement exists to avoid.
    """
    violations = [
        str(source.relative_to(REPO_ROOT))
        for source in _python_sources(BACKEND, SENSORS)
        if RADAR_ADAPTERS not in source.parents
        and source
        not in {
            SENSORS / "live.py",
            SENSORS / "tests" / "test_stub_radar_adapter.py",
        }
        and "stub_radar_adapter" in source.read_text()
    ]

    assert not violations, (
        "the stub radar must be importable from the composition point only; "
        f"these modules name it directly: {violations}"
    )


def test_only_the_composition_point_names_a_concrete_adapter() -> None:
    """ADR: the sensor set is assembled in exactly one place.

    `sensors/live.py` is the file edited when a sensor is added, removed or replaced. If any
    other module — `backend/main.py` above all — names a concrete adapter class, then swapping
    a sensor stops being a one-file change and nobody discovers that until the hardware is on
    the bench and the migration is larger than planned.

    The simulated adapter is exempt: it is driven by a scenario rather than by sensor
    configuration, so it is assembled on its own path.
    """
    concrete_adapters = ("CameraAdapter", "StubRadarAdapter", "Ld2451RadarAdapter")
    composition_point = SENSORS / "live.py"

    allowed = {
        composition_point,
        REPO_ROOT / "tests" / "unit" / "test_module_boundaries.py",
        # Its own package exports and its own tests.
        SENSORS / "adapters" / "camera" / "__init__.py",
        SENSORS / "adapters" / "camera" / "camera_adapter.py",
        SENSORS / "adapters" / "radar" / "__init__.py",
        SENSORS / "adapters" / "radar" / "stub_radar_adapter.py",
        SENSORS / "adapters" / "radar" / "ld2451_radar_adapter.py",
        SENSORS / "tests" / "test_camera_adapter.py",
        SENSORS / "tests" / "test_stub_radar_adapter.py",
        SENSORS / "tests" / "manual_camera_test.py",
        SENSORS / "tests" / "test_video_capture_frame_source.py",
    }

    violations: list[str] = []
    for source in _python_sources(BACKEND, SENSORS):
        if source in allowed:
            continue
        text = source.read_text()
        for name in concrete_adapters:
            if name in text:
                violations.append(f"{source.relative_to(REPO_ROOT)} names {name}")

    assert not violations, (
        "concrete sensor adapters must be named only in sensors/live.py, so that replacing a "
        "sensor is a change to that one file:\n  " + "\n  ".join(violations)
    )


def test_every_site_file_loads() -> None:
    """A site nobody loads is a site that has quietly rotted.

    Site files are strict-validated at startup, so a malformed one fails the run rather than
    degrading it. That is the right behaviour and it means a broken site is only discovered
    when somebody tries to use it — on the bench, typically. This loads all of them.
    """
    from backend.zones.geometry import load_site_geometry

    sites = sorted((REPO_ROOT / "config" / "sites").glob("*.json"))
    assert sites, "no site configurations found"

    for path in sites:
        geometry = load_site_geometry(path.stem)
        assert geometry.site.site_id == path.stem, (
            f"{path.name} declares site_id {geometry.site.site_id!r}; the filename is what "
            "--site takes, so a mismatch means the file a reviewer reads is not the one loaded"
        )
