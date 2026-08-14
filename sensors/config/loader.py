"""Loading and validating `sensor_config.yaml`.

Deployment facts about physical sensors — stream addresses, mounting geometry, thresholds,
identifiers — are configuration, never code, for the same reason the simulated sensor registry
is: changing where a camera points must not be a code change.

Loading is strict. An unknown key is an error rather than a silently ignored line, because
the failure mode of a tolerant loader is a site running with a threshold nobody set, which
looks exactly like a site running with the threshold somebody meant to set.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "sensor_config.yaml"

SENSOR_CONFIG_SCHEMA_VERSION = "1.0.0"


class SensorConfigError(RuntimeError):
    """The sensor configuration is missing, unreadable, or does not describe what it claims."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SiteOrigin(_Strict):
    """Anchor of the site's local ENU frame. Altitude is metres above the WGS-84 ellipsoid."""

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    altitude_m: float


class PositionEnu(_Strict):
    """Where a unit sits relative to the site origin, in local ENU metres."""

    east_m: float = 0.0
    north_m: float = 0.0
    up_m: float = 0.0


class Range(_Strict):
    """An inclusive closed interval a value is drawn from."""

    min: float
    max: float

    @model_validator(mode="after")
    def _ordered(self) -> Range:
        if self.max < self.min:
            raise ValueError(f"range max ({self.max}) is below min ({self.min})")
        return self


class StubRadarConfig(_Strict):
    """Emission behaviour of the stub radar. Read by no other adapter."""

    emission_probability: float = Field(ge=0.0, le=1.0)
    min_interval_s: float = Field(ge=0.0)
    bearing_deg: Range
    range_m: Range
    velocity_mps: Range
    confidence: Range
    position_uncertainty_m: float = Field(gt=0.0)
    seed: int | None = None

    @model_validator(mode="after")
    def _within_schema_bounds(self) -> StubRadarConfig:
        if self.confidence.min < 0.0 or self.confidence.max > 1.0:
            raise ValueError("confidence range must lie within [0, 1]")
        if self.range_m.min < 0.0:
            raise ValueError("range_m must be non-negative")
        if self.velocity_mps.min < 0.0:
            raise ValueError("velocity_mps must be non-negative")
        if self.bearing_deg.min < 0.0 or self.bearing_deg.max > 360.0:
            raise ValueError("bearing_deg range must lie within [0, 360]")
        return self


class RadarConfig(_Strict):
    sensor_id: str = Field(min_length=1, max_length=64)
    position_enu: PositionEnu = PositionEnu()
    boresight_deg: float = Field(default=0.0, ge=0.0, lt=360.0)
    stub: StubRadarConfig


class MotionConfig(_Strict):
    min_area_px: int = Field(gt=0)
    sensitivity_threshold: int = Field(ge=1, le=255)
    background_history: int = Field(gt=0)
    background_var_threshold: float = Field(gt=0.0)


class ClassifierConfig(_Strict):
    model_name: str = Field(min_length=1)
    confidence_threshold: float = Field(ge=0.0, le=1.0)
    inference_size_px: int = Field(gt=0)


class CameraConfig(_Strict):
    sensor_id: str = Field(min_length=1, max_length=64)
    stream_url: str = Field(min_length=1)
    frame_timeout_s: float = Field(gt=0.0)
    position_enu: PositionEnu = PositionEnu()
    boresight_deg: float = Field(default=0.0, ge=0.0, lt=360.0)
    horizontal_fov_deg: float = Field(gt=0.0, le=180.0)
    assumed_range_m: float = Field(gt=0.0)
    assumed_range_uncertainty_m: float = Field(gt=0.0)
    motion: MotionConfig
    classifier: ClassifierConfig


class SensorConfig(_Strict):
    """The whole document."""

    schema_version: str
    site_origin: SiteOrigin
    radar: RadarConfig
    camera: CameraConfig


def load_sensor_config(path: Path | str | None = None) -> SensorConfig:
    """Read and validate the sensor configuration document."""
    file = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        document = yaml.safe_load(file.read_text())
    except OSError as exc:
        raise SensorConfigError(f"could not read sensor config {file}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SensorConfigError(f"sensor config {file} is not valid YAML: {exc}") from exc

    if not isinstance(document, dict):
        raise SensorConfigError(f"sensor config {file} must be a mapping at the top level")

    try:
        config = SensorConfig.model_validate(document)
    except ValidationError as exc:
        raise SensorConfigError(f"sensor config {file} is invalid:\n{exc}") from exc

    if config.schema_version != SENSOR_CONFIG_SCHEMA_VERSION:
        raise SensorConfigError(
            f"sensor config {file} declares schema_version {config.schema_version!r}, "
            f"this build reads {SENSOR_CONFIG_SCHEMA_VERSION!r}"
        )
    return config
