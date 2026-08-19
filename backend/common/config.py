"""Configuration loading: JSON documents on disk into validated models.

A misconfigured deployment must fail at startup with a message that names the file and the
field, not half-load and behave strangely three hours into a run. Every loader here therefore
raises `ConfigError` rather than returning a partial object or a default, and every message
carries the path of the document that caused it.

This module is generic on purpose. It knows how to find, read and validate a configuration
document against a Pydantic model; it knows nothing about sites, zones, sensors or risk
geometry. `backend/common` may not import any other backend module (ADR 0009), so the
site-specific loader lives in `backend/zones/geometry.py` and is parameterised by the machinery
here rather than the other way round.

Paths resolve relative to the repository root, never to the current working directory — a
process started from a different directory must load the same configuration. The root is
overridable so that tests and future deployments can point at a different tree.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CONFIG_SCHEMA_VERSION = "1.0.0"

ModelT = TypeVar("ModelT", bound=BaseModel)

CONFIG_ROOT_ENV_VAR = "AETOS_CONFIG_ROOT"
"""Overrides the configuration tree location. Deployments mount config outside the source
tree; an explicit `ConfigPaths` argument still wins over this, so tests never depend on the
ambient environment."""

_DOCUMENT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ConfigError(RuntimeError):
    """Configuration is missing, unreadable, or does not satisfy its model.

    One exception type for every way loading can fail, because the caller's correct response
    is the same in all of them: refuse to start. A system that runs on configuration it could
    not fully validate is a system whose behaviour nobody can predict from its config files.
    """


@dataclass(frozen=True, slots=True)
class ConfigPaths:
    """Where configuration lives.

    Passed explicitly wherever a caller needs to redirect it. The default is derived from this
    file's own location rather than from `os.getcwd()`, so loading does not depend on where
    the process was started.
    """

    repo_root: Path
    config_root: Path

    @classmethod
    def default(cls) -> ConfigPaths:
        repo_root = Path(__file__).resolve().parents[2]
        override = os.environ.get(CONFIG_ROOT_ENV_VAR)
        config_root = Path(override).expanduser() if override else repo_root / "config"
        return cls(repo_root=repo_root, config_root=config_root)

    @property
    def sites(self) -> Path:
        return self.config_root / "sites"

    @property
    def environments(self) -> Path:
        return self.config_root / "environments"

    @property
    def sensor_registry(self) -> Path:
        return self.config_root / "sensor_registry"

    def site_file(self, name: str) -> Path:
        return self.sites / f"{_checked_name(name, 'site')}.json"

    def environment_file(self, name: str) -> Path:
        return self.environments / f"{_checked_name(name, 'environment')}.json"

    def sensor_registry_file(self, registry_key: str) -> Path:
        """The registry document describing one sensor model. One file per key, named by it."""
        return self.sensor_registry / f"{_checked_name(registry_key, 'sensor registry')}.json"

    def resolve(self, configured_path: str) -> Path:
        """Interpret a path written in a configuration document.

        Relative paths are relative to the repository root, not to the working directory, for
        the same reason the configuration tree is: two processes started from different
        directories must agree on which file they mean.
        """
        path = Path(configured_path).expanduser()
        return path if path.is_absolute() else self.repo_root / path


def _checked_name(name: str, kind: str) -> str:
    """Reject a document name that is not a plain identifier.

    Names arrive from configuration files and from the environment, and a name shaped like
    `../../etc/shadow` must produce a clear error rather than quietly reading whatever it
    points at.
    """
    if not _DOCUMENT_NAME.match(name):
        raise ConfigError(
            f"{name!r} is not a valid {kind} name: expected lower-case letters, digits, "
            f"underscore or hyphen, starting with a letter or digit"
        )
    return name


def load_config(model: type[ModelT], path: Path) -> ModelT:
    """Read one JSON document and validate it against `model`.

    The single entry point for every configuration load in the system, so that malformed
    configuration reports itself the same way wherever it appears.
    """
    document = _read_json(path)
    if not isinstance(document, dict):
        raise ConfigError(
            f"{path}: expected a JSON object at the top level, found "
            f"{type(document).__name__}"
        )

    try:
        return model.model_validate(document)
    except ValidationError as exc:
        raise ConfigError(_describe_validation_failure(path, model, exc)) from exc


def _read_json(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: configuration file not found") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: could not be read: {exc}") from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path}: line {exc.lineno} column {exc.colno}: invalid JSON: {exc.msg}"
        ) from exc


def _describe_validation_failure(
    path: Path, model: type[BaseModel], exc: ValidationError
) -> str:
    """Turn a Pydantic error into something an operator can act on without reading a stack
    trace: the file, the model it failed against, and every offending field by name."""
    problems = exc.errors()
    lines = [
        f"{path}: {len(problems)} problem(s) validating {model.__name__}:",
    ]
    for problem in problems:
        field = ".".join(str(part) for part in problem["loc"]) or "<document root>"
        lines.append(f"  {field}: {problem['msg']}")
    return "\n".join(lines)


class RunMode(StrEnum):
    """Where detections come from.

    Only SIMULATION is implemented. LIVE and HYBRID are named here so that configuration does
    not have to change shape when real adapters exist — mode selection is adapter registry
    configuration rather than a code path (docs/architecture.md, "Run modes"). Declaring LIVE
    today buys nothing: no real sensor integration exists, and whatever wires adapters is
    responsible for saying so. Config loading does not pretend to be that check.
    """

    SIMULATION = "simulation"
    LIVE = "live"
    HYBRID = "hybrid"


class QueueConfig(BaseModel):
    """Bounded inter-stage queue capacities and the shed policy's trip points (ADR 0008).

    Capacities are configuration rather than constants because the point at which a deployment
    cannot keep up depends on its hardware and its sensor count, and saturation must be a
    defined, observable state rather than an emergent one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ingestion_capacity: int = Field(gt=0)
    tracking_capacity: int = Field(gt=0)
    broadcast_capacity: int = Field(gt=0)

    shed_high_water: float = Field(gt=0.0, le=1.0)
    """Occupancy fraction at which shedding begins."""

    shed_resume_water: float = Field(ge=0.0, lt=1.0)
    """Occupancy fraction at which shedding stops. Strictly below `shed_high_water`: a single
    trip point makes the shed policy chatter at exactly the load where chattering is most
    expensive."""

    sustained_shed_window_s: float = Field(gt=0.0)
    """Window over which shedding is judged sustained rather than momentary."""

    degraded_mode_shed_fraction: float = Field(gt=0.0, le=1.0)
    """Fraction of inputs shed within that window which activates degraded mode."""

    @model_validator(mode="after")
    def _resume_below_high_water(self) -> QueueConfig:
        if self.shed_resume_water >= self.shed_high_water:
            raise ValueError(
                f"shed_resume_water ({self.shed_resume_water}) must be below shed_high_water "
                f"({self.shed_high_water}); equal values give no hysteresis and the shed "
                f"policy oscillates"
            )
        return self


class RiskLevelBoundary(BaseModel):
    """One Schmitt-trigger boundary between two risk levels (ADR 0007).

    `level` is the level entered when the score rises through `rise_score`. It is a plain
    string rather than an enum member because `backend/risk` owns the level vocabulary and
    `backend/common` may not import it; the risk engine checks these names against its own
    enum when it loads them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: str = Field(min_length=1, max_length=32)
    rise_score: float = Field(ge=0.0, le=1.0)
    fall_score: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _fall_below_rise(self) -> RiskLevelBoundary:
        if self.fall_score >= self.rise_score:
            raise ValueError(
                f"boundary {self.level}: fall_score ({self.fall_score}) must be below "
                f"rise_score ({self.rise_score}); without a gap there is no hysteresis band "
                f"and a score sitting on the boundary flaps every cycle"
            )
        return self


class RiskHysteresisConfig(BaseModel):
    """Dual thresholds plus minimum dwell, per ADR 0007."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    boundaries: list[RiskLevelBoundary] = Field(min_length=1)

    escalation_dwell_s: float = Field(ge=0.0)
    """Minimum time a level is held before escalating again."""

    de_escalation_dwell_s: float = Field(ge=0.0)
    """Minimum time before de-escalating. Longer than the escalation dwell: a genuine threat
    must not be held back by a de-escalation timer, but a single quiet update must not clear
    an alert either."""

    @model_validator(mode="after")
    def _dwell_is_asymmetric(self) -> RiskHysteresisConfig:
        if self.escalation_dwell_s > self.de_escalation_dwell_s:
            raise ValueError(
                f"escalation_dwell_s ({self.escalation_dwell_s}) must not exceed "
                f"de_escalation_dwell_s ({self.de_escalation_dwell_s}); ADR 0007 requires "
                f"escalation to be the faster direction"
            )

        scores = [b.rise_score for b in self.boundaries]
        if scores != sorted(scores) or len(set(scores)) != len(scores):
            raise ValueError(
                "boundaries must be ordered by strictly increasing rise_score, lowest first"
            )

        levels = [b.level for b in self.boundaries]
        duplicates = {level for level in levels if levels.count(level) > 1}
        if duplicates:
            raise ValueError(f"duplicate risk level in boundaries: {sorted(duplicates)}")
        return self


class RiskConfig(BaseModel):
    """Risk tuning that belongs to a deployment rather than to the code.

    **Not read by the risk engine. `config/risk/policy.json` is the live tuning.**

    This block was written before `backend/risk` existed, against the expectation that the
    engine would read its hysteresis from the environment document. It did not: the engine
    loads `config/risk/policy.json` and takes its bands, thresholds and dwell times entirely
    from there. Nothing at runtime constructs an `EnvironmentConfig` at all.

    That leaves two documents describing the same tuning in different units — `rise_score` here
    is a fraction, `rise` there is a score out of 100 — with different values and no error from
    either. Editing this one changes nothing, silently, which is the failure `RiskPolicy`'s
    strict validation exists to prevent for the file that *is* read: a config format that
    quietly drops what it does not understand is how a site ends up running tuning nobody
    applied. Validating cleanly while being ignored is a worse version of the same trap.

    It is left in place rather than deleted because the schema, its validation and its tests are
    sound, and reconciling the two representations is a deliberate decision about which units
    the deployment surface should speak — not something to settle as a side effect of a
    docstring correction. Until that decision is made, this is documentation of an intent, and
    the docstring says so where someone reading the model will see it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hysteresis: RiskHysteresisConfig


class EnvironmentConfig(BaseModel):
    """One deployment environment: which site, which sensors, which knobs.

    Environments are separated from day one so that a simulation-only tuning change cannot
    silently become the value a future staging or production deployment runs on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = CONFIG_SCHEMA_VERSION
    environment: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=256)

    run_mode: RunMode

    site: str = Field(min_length=1, max_length=64)
    """Name of the document in `config/sites/`, without the `.json` suffix."""

    enabled_sensor_keys: list[str] | None = None
    """Registry keys this environment permits, or None for every key the site references.

    An environment restricts the registry; it never extends it. The registry documents
    themselves are owned by the sensor layer and are deliberately not parsed here —
    `backend/common` has no business knowing what a sensor's characteristics look like, and
    which keys a run actually uses depends on the site's placements as well as on this list."""

    event_store_path: str | None = None
    """Where the append-only log lives, relative to the repository root unless absolute. None
    selects an in-memory log, which has no durability and is only ever appropriate for tests."""

    queues: QueueConfig
    risk: RiskConfig

    def permits_registry_key(self, registry_key: str) -> bool:
        """Whether a sensor placement referencing this registry key runs in this environment."""
        return self.enabled_sensor_keys is None or registry_key in self.enabled_sensor_keys


def available_environments(paths: ConfigPaths | None = None) -> list[str]:
    """Names of the environments configured in this tree, sorted."""
    paths = paths or ConfigPaths.default()
    if not paths.environments.is_dir():
        return []
    return sorted(p.stem for p in paths.environments.glob("*.json"))


def load_environment(name: str, paths: ConfigPaths | None = None) -> EnvironmentConfig:
    """Load one environment by name.

    The document's own `environment` field must agree with the file name. A copied config file
    whose name was updated and whose contents were not is otherwise indistinguishable from the
    environment it claims to be, which is precisely the mistake that puts simulation tuning
    into a staging run.
    """
    paths = paths or ConfigPaths.default()
    path = paths.environment_file(name)

    if not path.is_file():
        known = ", ".join(available_environments(paths)) or "none found"
        raise ConfigError(f"{path}: no such environment {name!r}; available: {known}")

    environment = load_config(EnvironmentConfig, path)
    if environment.environment != name:
        raise ConfigError(
            f"{path}: environment: declares {environment.environment!r} but was loaded as "
            f"{name!r}; the file name and the declared name must agree"
        )
    return environment


def site_config_path(environment: EnvironmentConfig, paths: ConfigPaths | None = None) -> Path:
    """Which site document this environment runs against."""
    paths = paths or ConfigPaths.default()
    return paths.site_file(environment.site)


def available_sensor_registry_keys(paths: ConfigPaths | None = None) -> list[str]:
    """Keys the registry defines in this tree, sorted. The registry is one document per sensor
    model, so the file names are the keys."""
    paths = paths or ConfigPaths.default()
    if not paths.sensor_registry.is_dir():
        return []
    return sorted(p.stem for p in paths.sensor_registry.glob("*.json"))


def sensor_registry_entry_path(registry_key: str, paths: ConfigPaths | None = None) -> Path:
    """Where the document for one registry key lives.

    Only the path. Reading it is the sensor layer's job — a sensor's range, field of view and
    uncertainty are not primitives, and `backend/common` does not model them.
    """
    paths = paths or ConfigPaths.default()
    return paths.sensor_registry_file(registry_key)


def event_store_path(
    environment: EnvironmentConfig, paths: ConfigPaths | None = None
) -> Path | None:
    """Absolute path of this environment's event log, or None for an in-memory log."""
    paths = paths or ConfigPaths.default()
    if environment.event_store_path is None:
        return None
    return paths.resolve(environment.event_store_path)
