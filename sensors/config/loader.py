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


class SerialConfig(_Strict):
    """How to reach a serial device. The one thing that changes per host."""

    port: str = Field(min_length=1)
    """Device path or name. `/dev/ttyUSB0` on Linux, `COM3` on Windows."""

    baud: int = Field(gt=0)
    read_timeout_s: float = Field(gt=0.0)
    """How long a read blocks before returning what it has. Bounds how long shutdown waits."""

    reconnect_backoff_s: float = Field(default=1.0, gt=0.0)


class RadarCoverage(_Strict):
    """What the unit can physically observe. Used to reject returns it could not have made.

    A radar reporting a target beyond its own maximum range is reporting a decode error or a
    multipath artefact, not a target. Bounding it here means the adapter discards it rather
    than the pipeline tracking a contact that cannot exist.
    """

    min_range_m: float = Field(ge=0.0)
    max_range_m: float = Field(gt=0.0)
    horizontal_fov_deg: float = Field(gt=0.0, le=360.0)

    min_speed_mps: float = Field(default=0.0, ge=0.0)
    """Doppler floor. A target slower than this is invisible to the sensor however close it is.

    Not a filter but a statement of capability: it is why a hovering target produces nothing,
    and recording it is what stops that silence being read as an empty sky (FM-010)."""

    max_speed_mps: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _ordered(self) -> RadarCoverage:
        if self.max_range_m <= self.min_range_m:
            raise ValueError("max_range_m must exceed min_range_m")
        if self.max_speed_mps <= self.min_speed_mps:
            raise ValueError("max_speed_mps must exceed min_speed_mps")
        return self


class RadarUncertainty(_Strict):
    """How wrong the unit's measurements are, per axis, in its own frame.

    Separate axes because a radar's error is strongly anisotropic — precise in range, coarse in
    cross-range — and collapsing that into one number makes the downstream filter confident in
    a direction the sensor is not. Mirrors the simulated sensor model's `Uncertainty` so the
    two describe a sensor the same way.
    """

    range_sigma_m: float = Field(gt=0.0)
    """One-sigma along the line of sight. A radar's strongest axis."""

    cross_range_sigma_m: float = Field(gt=0.0)
    """Floor on cross-range error, independent of range."""

    cross_range_sigma_mrad: float = Field(gt=0.0)
    """Angular component, which grows the cross-range error with range. Combined with the floor
    above rather than replacing it: a constant would be wrong at both ends of the envelope."""

    vertical_sigma_m: float = Field(gt=0.0)
    """One-sigma on height.

    For a unit with no elevation channel this is not a measurement error, it is the depth of
    the system's ignorance: the target is somewhere within the sensor's vertical beam and
    nothing narrows it further. It should be of the same order as the maximum range, and
    stating it honestly is what stops the filter inventing an altitude."""

    def cross_range_sigma_at(self, range_m: float) -> float:
        """Cross-range one-sigma at a given range, floor and angular term combined."""
        return max(self.cross_range_sigma_m, range_m * self.cross_range_sigma_mrad / 1000.0)


class RadarSignal(_Strict):
    """Mapping the device's native signal-strength figure onto a confidence.

    The device reports an integer on a scale of its own. It is not decibels and must not be
    reported as `snr_db`; this block is what turns it into the 0-1 confidence the schema
    defines, on a scale somebody has actually checked.
    """

    strength_min: int = Field(ge=0, le=255)
    """Reported strength mapping to the lowest confidence. Below it, targets are discarded."""

    strength_max: int = Field(ge=0, le=255)
    confidence_min: float = Field(ge=0.0, le=1.0)
    confidence_max: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> RadarSignal:
        if self.strength_max <= self.strength_min:
            raise ValueError("strength_max must exceed strength_min")
        if self.confidence_max <= self.confidence_min:
            raise ValueError("confidence_max must exceed confidence_min")
        return self

    def confidence_for(self, strength: int) -> float:
        """Linear interpolation, clamped. Never 1.0 unless configured so — a radar return is
        evidence of an object, not proof of one."""
        span = self.strength_max - self.strength_min
        fraction = (strength - self.strength_min) / span
        fraction = min(1.0, max(0.0, fraction))
        return self.confidence_min + fraction * (self.confidence_max - self.confidence_min)


class RadarConfig(_Strict):
    sensor_id: str = Field(min_length=1, max_length=64)
    position_enu: PositionEnu = PositionEnu()
    boresight_deg: float = Field(default=0.0, ge=0.0, lt=360.0)
    stub: StubRadarConfig

    serial: SerialConfig | None = None
    """Absent while the stub is in use. Required by any adapter that talks to real hardware,
    which raises rather than inventing a port if it is missing."""

    coverage: RadarCoverage | None = None
    uncertainty: RadarUncertainty | None = None
    signal: RadarSignal | None = None


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
