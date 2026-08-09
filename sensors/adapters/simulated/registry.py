"""Loading the sensor registry from `config/sensor_registry/`.

Sensor characteristics are configuration, never code. A site's `SensorPlacement` names a
`registry_key`; this module turns that key into the `SensorModel` the simulated adapter
degrades truth through.

Loading is strict: an unreadable file, a malformed document or a key that disagrees with its
filename all raise. A registry that half-loads would leave a site running with a sensor whose
characteristics are not the ones anybody configured.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from sensors.adapters.simulated.sensor_model import SensorModel

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[3] / "config" / "sensor_registry"


class SensorRegistryError(RuntimeError):
    """The sensor registry could not be loaded or does not describe what it claims to."""


def load_sensor_registry(path: Path | str | None = None) -> dict[str, SensorModel]:
    """Every `*.json` entry in the registry directory, keyed by `registry_key`."""
    directory = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    if not directory.is_dir():
        raise SensorRegistryError(f"sensor registry directory not found: {directory}")

    models: dict[str, SensorModel] = {}
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


def load_sensor_model(file: Path | str) -> SensorModel:
    """One registry entry. The filename must match the key it declares.

    The filename check is not pedantry: `registry_key` is what a site references, and a file
    named one thing while declaring another means the entry a reviewer reads is not the entry
    the system loads.
    """
    file = Path(file)
    try:
        document = json.loads(file.read_text())
    except OSError as exc:
        raise SensorRegistryError(f"could not read sensor registry entry {file}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SensorRegistryError(f"sensor registry entry {file} is not valid JSON: {exc}") from exc

    try:
        model = SensorModel.model_validate(document)
    except ValidationError as exc:
        raise SensorRegistryError(f"sensor registry entry {file} is invalid:\n{exc}") from exc

    if model.registry_key != file.stem:
        raise SensorRegistryError(
            f"sensor registry entry {file} declares registry_key {model.registry_key!r}, "
            f"which does not match its filename"
        )
    return model
