"""What a physically fitted radar is: link settings, geometry, and honest error figures.

A real sensor needs two kinds of parameter and they are not the same kind of thing. The link
settings (`port`, `baud_rate`) are properties of *this* installation and change when someone
plugs the adapter into a different socket. The measurement parameters (beamwidth, range and
bearing sigmas) are properties of the *device*, and they are the numbers the tracking filter
believes. Both live in one document per fitted sensor under `config/hardware/`, keyed the same
way the simulated registry is keyed, so that a site placement names a `registry_key` and gets
either a simulation profile or a real device depending only on which directory the key is
found in.

**These numbers are not a datasheet.** The vendor publishes a rated range and an azimuth
extent for vehicle-sized targets and nothing about bearing accuracy, elevation beamwidth or
range error, and it says nothing at all about performance against a small airframe. The
defaults here are deliberately pessimistic placeholders, and the honest thing an operator can
do with them is measure the device against a known target and write down what they measured.
The bench procedure in `docs/hardware_radar_bench.md` says how. Understating uncertainty here
makes the filter overconfident, which is worse than reporting none at all — it is the adapter
obligation in `docs/detection_schema.md` that this file exists to satisfy.
"""

from __future__ import annotations

import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.common.config import ConfigError, ConfigPaths, load_config
from sensors.schemas.detection import SensorType

RADAR_HARDWARE_SCHEMA_VERSION = "1.0.0"


class LinkConfig(BaseModel):
    """How to reach the device on this machine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: str = Field(min_length=1, max_length=256)
    """Serial device path: `/dev/ttyUSB0` on Linux, `/dev/cu.usbserial-*` on macOS, `COM3` on
    Windows. Installation-specific by nature, which is why it is configuration and not code."""

    baud_rate: int = Field(default=115200, gt=0)
    read_timeout_s: float = Field(default=0.2, gt=0.0, le=5.0)
    """How long a single read blocks before returning empty. Bounds how long the reader thread
    takes to notice `stop()`, so it is kept short."""

    silence_timeout_s: float = Field(default=5.0, gt=0.0)
    """How long the link may deliver *no bytes at all* before the adapter reports itself
    degraded.

    This is not a detection timeout, and the difference is the whole point. The device frames
    a report on its own cadence whether or not anything is in front of it, so a link carrying
    empty reports is a sensor observing an empty sky, while a link carrying nothing is a
    sensor that has stopped talking to us. Only the second is a fault, and only the second is
    what this bounds (FM-010)."""


class ProtocolConfig(BaseModel):
    """The two wire conventions the device's documentation does not settle.

    Both are sign conventions, both are ten seconds to determine on a bench, and both are
    silently wrong in a way that looks plausible if guessed. See `docs/hardware_radar_bench.md`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    approaching_code: int = Field(default=1, ge=0, le=1)
    """Value of the direction byte meaning the target is closing on the sensor."""

    azimuth_sign: int = Field(default=1)
    """+1 if a positive reported azimuth is clockwise from boresight, -1 if anticlockwise.

    Getting this backwards mirrors the picture about the boresight axis: tracks appear on the
    wrong side of the sensor, which on a symmetric site looks entirely reasonable."""

    @model_validator(mode="after")
    def _sign_is_a_sign(self) -> ProtocolConfig:
        if self.azimuth_sign not in (1, -1):
            raise ValueError("azimuth_sign must be +1 or -1")
        return self


class RadarCoverage(BaseModel):
    """The volume the device can report from, as a gate on what is believed.

    Applied to reports that arrive, rather than used to predict what should: a report from
    beyond the configured maximum range is not a distant target, it is a decode error or a
    device configured differently from this document, and admitting it would put a phantom on
    the operator's map.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_range_m: float = Field(gt=0.0, le=1000.0)
    min_range_m: float = Field(default=0.0, ge=0.0)
    azimuth_extent_deg: float = Field(gt=0.0, le=360.0)
    """Total azimuth width the device reports over, centred on boresight."""

    elevation_beamwidth_deg: float = Field(gt=0.0, le=180.0)
    """Total elevation width of the beam.

    The device reports **no elevation**. Every detection it produces is placed at the centre
    of this beam and given a vertical uncertainty derived from its width, which is the only
    honest thing to do with an unmeasured dimension: the object is somewhere in the beam and
    the adapter does not know where. See `adapter.py`."""

    @model_validator(mode="after")
    def _ranges_are_ordered(self) -> RadarCoverage:
        if self.min_range_m >= self.max_range_m:
            raise ValueError("min_range_m must be below max_range_m")
        return self


class RadarUncertainty(BaseModel):
    """One-sigma measurement error. What the filter will believe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    range_sigma_m: float = Field(gt=0.0)
    """Along the line of sight. Cannot honestly be below the device's own reporting
    resolution, which is one whole metre."""

    azimuth_sigma_deg: float = Field(gt=0.0, le=90.0)
    """Bearing error, which becomes a cross-range error growing linearly with range: at 100 m
    a 3 degree sigma is already 5 m across."""

    radial_velocity_sigma_mps: float = Field(gt=0.0)
    """Error on the reported speed, which the device sends quantised to whole km/h."""


class QualityMapping(BaseModel):
    """How the device's unitless signal level becomes a confidence in [0, 1].

    A mapping rather than a passthrough because `Quality.confidence` has a defined meaning —
    the source's confidence that this is a real object — and a device-specific level does not
    carry that meaning on its own. The mapping is linear, clamped, and configured, so that
    what the operator interface renders as confidence can be traced to a number somebody
    chose rather than to an accident of scaling.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_level_full_confidence: int = Field(default=60, gt=0, le=255)
    """Level at or above which `max_confidence` is reported."""

    min_confidence: float = Field(default=0.2, ge=0.0, le=1.0)
    max_confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    """Deliberately below 1.0. A single-sensor return from a device rated for vehicles and
    unverified against small airframes is never certain, and a confidence of 1.0 would say it
    was."""

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> QualityMapping:
        if self.min_confidence > self.max_confidence:
            raise ValueError("min_confidence must not exceed max_confidence")
        return self

    def confidence_for(self, signal_level: int) -> float:
        fraction = min(max(signal_level / self.signal_level_full_confidence, 0.0), 1.0)
        return self.min_confidence + fraction * (self.max_confidence - self.min_confidence)


class RadarHardwareConfig(BaseModel):
    """One physically fitted radar, completely described."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = RADAR_HARDWARE_SCHEMA_VERSION
    registry_key: str = Field(min_length=1, max_length=64)
    sensor_type: SensorType = SensorType.RADAR
    description: str = Field(min_length=1, max_length=512)

    link: LinkConfig
    protocol: ProtocolConfig = ProtocolConfig()
    coverage: RadarCoverage
    uncertainty: RadarUncertainty
    quality: QualityMapping = QualityMapping()

    tilt_deg: float = Field(default=0.0, ge=-90.0, le=90.0)
    """Elevation of the boresight above horizontal, from physically tilting the mount.

    The device has no elevation output, so tilting the housing is the only way to cover
    airspace above the horizon, and the tilt has to be written down here because the adapter
    cannot infer it. A tilt in the housing that is not in this file puts every detection at
    the wrong height by `range * sin(error)`, which at 100 m and 10 degrees is 17 m."""

    report_interval_s: float = Field(default=0.1, gt=0.0)
    """Nominal interval between the device's own reports. Used only to size the reader's
    hand-off queue; the pipeline's cadence is set by the pipeline."""

    @model_validator(mode="after")
    def _is_a_real_sensor_type(self) -> RadarHardwareConfig:
        if self.sensor_type.is_simulated:
            raise ValueError(
                f"{self.registry_key}: a hardware adapter must not declare a simulated "
                f"sensor_type ({self.sensor_type}). The simulated/real distinction is carried "
                "by SensorRef.sensor_type and an operator's whole reading of the display "
                "depends on it."
            )
        return self

    @property
    def half_azimuth_rad(self) -> float:
        return math.radians(self.coverage.azimuth_extent_deg) / 2.0


def load_radar_hardware(registry_key: str, paths: ConfigPaths | None = None) -> RadarHardwareConfig:
    """Load one fitted-sensor document by key. The filename must match the key it declares.

    Same rule, and the same reason, as the simulated registry: `registry_key` is what a site
    placement references, and a file named one thing while declaring another means the
    document a reviewer reads is not the document the system runs.
    """
    resolved = paths or ConfigPaths.default()
    file = resolved.hardware_file(registry_key)
    config = load_config(RadarHardwareConfig, file)
    if config.registry_key != registry_key:
        raise ConfigError(
            f"{file} declares registry_key {config.registry_key!r}, which does not match its "
            "filename"
        )
    return config


def hardware_keys(paths: ConfigPaths | None = None) -> frozenset[str]:
    """Every key `config/hardware/` defines, or an empty set if the directory is absent.

    Absent is not an error: a deployment with no hardware fitted is the normal case, and it is
    how the pipeline decides a placement refers to a simulated profile rather than a real one.
    """
    resolved = paths or ConfigPaths.default()
    directory: Path = resolved.hardware
    if not directory.is_dir():
        return frozenset()
    return frozenset(file.stem for file in directory.glob("*.json"))
