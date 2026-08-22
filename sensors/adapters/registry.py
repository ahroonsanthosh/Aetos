"""The sensor registry: one document per sensor model, of one of two kinds.

A site's `SensorPlacement` names a `registry_key`; this turns that key into the model describing
what the sensor is. Sensor characteristics are configuration, never code — the same model fitted
at two sites must not require two builds.

## Why the entries are discriminated

Until live adapters existed, every registry document described *simulation parameters*: the
noise, dropout and confusion the simulated adapter degrades ground truth through. Its own module
docstring is emphatic that none of it is a measurement of, a specification for, or a claim about
any real device.

A live driver's document is the opposite in every respect. Its numbers are claims about real
hardware, it carries a driver name and connection parameters, and it has nothing to say about
false-alarm rates or classification confusion because those are not things you configure on a
sensor you did not build.

Loading both through one model would mean a document full of optional fields where the
combination that is actually valid depends on a field that is not marked as deciding it — and
the first symptom would be a live sensor loading successfully with a simulated sensor's
probability of detection quietly applied to it. So `kind` is an explicit, required discriminator.

It is required rather than defaulted deliberately. An undeclared discriminator is exactly the
ambiguity this loader already refuses elsewhere: a document whose filename and declared key
disagree is rejected because the entry a reviewer reads must be the entry the system loads, and
a document that does not say what kind of thing it is has the same problem.

## Loading is strict

An unreadable file, malformed JSON, a key that disagrees with its filename, or an unknown kind
all raise. A registry that half-loads would leave a site running with a sensor whose
characteristics are not the ones anybody configured.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from sensors.adapters.capabilities import (
    AccuracyDeclaration,
    Coverage,
    DimensionsDeclaration,
    TimingDeclaration,
)
from sensors.adapters.simulated.sensor_model import SensorModel
from sensors.schemas.detection import SensorType

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "sensor_registry"

REGISTRY_SCHEMA_VERSION = "1.1.0"
"""1.1.0 added the required `kind` discriminator and the live entry shape."""


class SensorRegistryError(RuntimeError):
    """The sensor registry could not be loaded or does not describe what it claims to."""


class RegistryEntryKind(StrEnum):
    """Which sort of thing a registry document describes."""

    SIMULATED = "simulated"
    """Simulation parameters. Consumed by `SimulatedSensorAdapter`; describes no real device."""

    LIVE = "live"
    """A real sensor: its declared characteristics, and which driver talks to it."""


class LiveSensorModel(BaseModel):
    """A real sensor's declared characteristics and how to reach it.

    Everything here is a **claim about hardware**, which is the opposite of what a simulated
    entry contains, and it is the claim a customer was shown. The conformance suite measures the
    adapter's behaviour against it (ADR 0017).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = REGISTRY_SCHEMA_VERSION
    kind: Literal["live"]
    registry_key: str = Field(min_length=1, max_length=64)
    sensor_type: SensorType
    description: str = Field(min_length=1, max_length=512)

    coverage: Coverage
    timing: TimingDeclaration
    accuracy: AccuracyDeclaration
    dimensions: DimensionsDeclaration

    classifies: bool = False
    coverage_validated: bool = False
    """False until someone has flown the actual threat class at known ranges. See FM-016."""

    driver: str = Field(min_length=1, max_length=64)
    """Which adapter implementation talks to this sensor. Resolved by
    `sensors/adapters/factory.py`, which knows the drivers that exist; the registry deliberately
    does not, so adding a driver does not mean editing the loader."""

    driver_config: dict[str, Any] = Field(default_factory=dict)
    """Driver-specific parameters — a serial port, a baud rate, a plausibility envelope.

    Deliberately untyped **here** and never consumed in this shape. The registry cannot know
    every driver's configuration, so it validates the part common to all sensors and the factory
    validates this part against the driver's own model, naming the file when it fails. A driver
    that accepted a raw mapping without validating it would push a configuration error into a
    runtime one, which is the failure mode this whole loader exists to prevent.

    Notably absent: anything about transport integrity. The residual corruption probability of a
    checksumless link is a property of the frame layout and the configured envelope, so it is
    *computed* by the decoder rather than asserted in a document, and the adapter builds its own
    `TransportDeclaration` at runtime (ADR 0018)."""

    @model_validator(mode="after")
    def _is_a_real_type(self) -> LiveSensorModel:
        if self.sensor_type.is_simulated:
            raise ValueError(
                f"registry entry {self.registry_key!r} is declared live but names sensor_type "
                f"{self.sensor_type.value!r}: a real adapter presenting itself as simulated "
                "would make the operator interface show real data as synthetic, and the "
                "simulated/real distinction is carried by this field alone"
            )
        return self


RegistryEntry = SensorModel | LiveSensorModel
"""What a registry key resolves to. Callers branch on the concrete type, which is what the
discriminator exists to make safe."""


def load_sensor_registry(path: Path | str | None = None) -> dict[str, RegistryEntry]:
    """Every `*.json` entry in the registry directory, keyed by `registry_key`."""
    directory = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    if not directory.is_dir():
        raise SensorRegistryError(f"sensor registry directory not found: {directory}")

    models: dict[str, RegistryEntry] = {}
    for file in sorted(directory.glob("*.json")):
        model = load_sensor_model(file)
        if model.registry_key in models:
            raise SensorRegistryError(
                f"duplicate registry_key {model.registry_key!r} in {directory}"
            )
        models[model.registry_key] = model

    if not models:
        raise SensorRegistryError(f"sensor registry at {directory} contains no entries")
    return models


def load_sensor_model(file: Path | str) -> RegistryEntry:
    """One registry entry, of whichever kind it declares itself to be.

    The filename check is not pedantry: `registry_key` is what a site references, and a file
    named one thing while declaring another means the entry a reviewer reads is not the entry
    the system loads.
    """
    file = Path(file)
    document = _read_document(file)
    kind = _declared_kind(file, document)

    # Branching on the discriminator rather than selecting a model class into a variable: the
    # two arms return genuinely different types, and narrowing them here is what lets the
    # filename check below read `model.registry_key` without the type system losing track.
    model: RegistryEntry
    try:
        if kind is RegistryEntryKind.SIMULATED:
            model = SensorModel.model_validate(document)
        else:
            model = LiveSensorModel.model_validate(document)
    except ValidationError as exc:
        raise SensorRegistryError(
            f"sensor registry entry {file} is invalid as a {kind.value!r} entry:\n{exc}"
        ) from exc

    if model.registry_key != file.stem:
        raise SensorRegistryError(
            f"sensor registry entry {file} declares registry_key {model.registry_key!r}, "
            "which does not match its filename"
        )
    return model


def _read_document(file: Path) -> dict[str, Any]:
    try:
        document = json.loads(file.read_text())
    except OSError as exc:
        raise SensorRegistryError(f"could not read sensor registry entry {file}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SensorRegistryError(
            f"sensor registry entry {file} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(document, dict):
        raise SensorRegistryError(
            f"sensor registry entry {file}: expected a JSON object at the top level, found "
            f"{type(document).__name__}"
        )
    return document


def _declared_kind(file: Path, document: dict[str, Any]) -> RegistryEntryKind:
    """Read the discriminator, refusing a document that does not carry one.

    No default. A missing `kind` would be read as simulated, which is the more permissive of the
    two and the one whose parameters mean nothing on real hardware — so the failure would be a
    live sensor silently loaded with a simulator's probability of detection and false-alarm rate
    applied to it, and nothing would say so.
    """
    raw = document.get("kind")
    if raw is None:
        raise SensorRegistryError(
            f"sensor registry entry {file} does not declare a 'kind'. Every entry must say "
            f"whether it describes simulation parameters ({RegistryEntryKind.SIMULATED.value!r}) "
            f"or a real sensor ({RegistryEntryKind.LIVE.value!r}); the two documents mean "
            "entirely different things and share almost no fields"
        )

    try:
        return RegistryEntryKind(raw)
    except ValueError as exc:
        known = ", ".join(repr(k.value) for k in RegistryEntryKind)
        raise SensorRegistryError(
            f"sensor registry entry {file} declares kind {raw!r}; expected one of {known}"
        ) from exc
