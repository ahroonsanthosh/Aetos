"""Configuration for the serial-attached FMCW radar adapter.

Every number the testbed unit forces on us lives here rather than in the driver, for two
reasons. The first is the usual one: sensor characteristics are configuration, never code
(`sensors/adapters/simulated/registry.py` makes the same argument for the simulator). The
second matters more here — several of these values are **unverified against the physical
unit**, and a value that has to be checked is far easier to check when it is a named field
with a docstring saying so than when it is a literal three call frames deep.

Fields whose docstring begins with UNVERIFIED are exactly that: a starting point, to be
confirmed by the first-run procedure in this package's README before any detection output is
trusted. None of them is a claim.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class UartLinkConfig(BaseModel):
    """The serial link to the sensor, through the USB-to-TTL adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: str = Field(min_length=1)
    """Serial device — `/dev/ttyUSB0`, `/dev/tty.SLAB_USBtoUART`, `COM4`."""

    baud: int = Field(default=115200, gt=0)
    """UNVERIFIED. Published sources disagree between 115200 and 256000 for this unit, and a
    wrong baud does not fail loudly — it delivers a byte stream that never syncs, which the
    parser reports as sustained framing errors rather than as silence. Confirm against the
    physical unit before deploying. `probe_baud_rates` is the list a bring-up sweep tries."""

    probe_baud_rates: tuple[int, ...] = (115200, 256000)
    """Candidate rates for the bring-up sweep. Not used in normal operation."""

    read_timeout_s: float = Field(default=0.05, gt=0.0)
    """Blocking-read budget per poll, and a direct tax on the ingestion cycle.

    `get_detections()` runs on the event loop thread, so a read that waits here is a read that
    delays every WebSocket broadcast and HTTP response for the same period. The arithmetic that
    matters: **live sensors × this timeout** must stay well under the pipeline tick (0.5 s by
    default), or the loop spends more time waiting on quiet ports than doing anything else.
    `backend/main.py` warns at start-up when a configuration crosses half the tick.

    In practice the sensor reports several times a second, so bytes are usually already
    buffered and the read returns at once. This budget is what bounds the *quiet* case, which
    is exactly when a stalled interface would be most misleading."""

    reconnect_backoff_s: float = Field(default=2.0, gt=0.0)
    """Delay before retrying a dropped link. A USB serial adapter that is unplugged and
    replugged reappears on the same path; retrying immediately in a tight loop would spend the
    ingestion cycle failing to open a device that is not there yet."""

    max_read_bytes: int = Field(default=4096, gt=0)
    """Ceiling on bytes consumed per poll, so a backlogged or babbling link cannot hold the
    ingestion cycle open indefinitely (FM-014 in the small)."""


class MountConfig(BaseModel):
    """Where the sensor is and which way its beam points.

    Placement duplicated from `SensorPlacement` rather than imported: this package must not
    depend on `backend.zones`, and the adapter is constructed with a `LocalFrame` and this
    block, not with a site.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    altitude_m: float = 0.0
    """Antenna altitude, metres above the WGS-84 ellipsoid — the same datum the canonical
    schema uses everywhere else. Not above ground level (FM-004)."""

    boresight_deg: float = Field(default=0.0, ge=0.0, lt=360.0)
    """Beam centreline, degrees clockwise from true north. The unit reports azimuth relative
    to its own PCB, so this is what turns a sensor-relative angle into a bearing."""

    tilt_deg: float = Field(default=0.0, ge=-90.0, le=90.0)
    """Mechanical up-tilt of the mount, degrees above horizontal.

    The sensor has **no elevation channel**. Tilting the mount rotates the whole beam upward,
    trading ground coverage for elevated coverage, and this value is the only thing that lets
    downstream code know which slice of sky was actually scanned. It is a first-class test
    variable, logged per session — never a fixed setup constant assumed to be zero."""

    azimuth_half_width_deg: float = Field(default=20.0, gt=0.0, le=90.0)
    """Half-extent of the azimuth beam. Reports outside it are dropped as framing noise."""

    elevation_ambiguity_deg: float = Field(default=30.0, gt=0.0, le=180.0)
    """UNVERIFIED. Full elevation extent of the beam, centred on `tilt_deg`.

    This is not a measurement, it is the *width of our ignorance*: with no elevation channel,
    a target anywhere in this wedge produces an identical report. It sets the vertical
    variance the adapter reports, which is how the tracker is told not to believe the
    altitude it is handed. Widening it is always safer than narrowing it."""


class UncertaintyConfig(BaseModel):
    """One-sigma measurement error, in the sensor's own along-/across-beam frame."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    range_sigma_m: float = Field(default=1.5, gt=0.0)
    """UNVERIFIED. Range is reported quantised to whole metres, so this cannot honestly be
    below about 0.3 m regardless of what the datasheet claims for the RF front end."""

    azimuth_sigma_deg: float = Field(default=3.0, gt=0.0)
    """UNVERIFIED. Angle is reported quantised to whole degrees; the true figure is dominated
    by the unit's angular resolution, not its quantisation, and is expected to be worse."""

    radial_velocity_sigma_mps: float = Field(default=0.5, gt=0.0)
    """UNVERIFIED. Speed arrives quantised to whole km/h (0.28 m/s)."""


class ConfidenceConfig(BaseModel):
    """How the unit's signal-quality byte becomes a canonical confidence.

    The byte is an uncalibrated integer, not a decibel figure and not a probability, so it is
    never emitted as `snr_db`. It is mapped monotonically onto a **deliberately capped** band:
    until the test matrix in this package's README has been run, no reading from this hardware
    justifies a high-confidence claim about a small airframe, and the cap is what stops the
    driver from making one on the classifier's behalf.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    quality_floor: int = Field(default=0, ge=0, le=255)
    quality_ceiling: int = Field(default=120, ge=1, le=255)
    """UNVERIFIED. Reading at or above this maps to `confidence_ceiling`."""

    confidence_floor: float = Field(default=0.10, ge=0.0, le=1.0)
    confidence_ceiling: float = Field(default=0.60, ge=0.0, le=1.0)
    """Capped well below 1.0 on purpose — see the class docstring."""

    @model_validator(mode="after")
    def _check_ordering(self) -> ConfidenceConfig:
        if self.quality_ceiling <= self.quality_floor:
            raise ValueError("quality_ceiling must exceed quality_floor")
        if self.confidence_ceiling < self.confidence_floor:
            raise ValueError("confidence_ceiling must not be below confidence_floor")
        return self


class DetectionGateConfig(BaseModel):
    """Limits applied to reports after parsing, before they become Detections."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_range_m: float = Field(default=10.0, ge=0.0)
    max_range_m: float = Field(default=100.0, gt=0.0)
    """Rated maximum for this unit is 100 m against vehicle-scale radar cross-section.
    Detection of a 135 g airframe at any range is unverified."""

    min_quality: int = Field(default=0, ge=0, le=255)
    """Reports below this signal-quality byte are discarded before emission."""

    @model_validator(mode="after")
    def _check_range_window(self) -> DetectionGateConfig:
        if self.max_range_m <= self.min_range_m:
            raise ValueError("max_range_m must exceed min_range_m")
        return self


class UartFmcwRadarConfig(BaseModel):
    """Everything the adapter needs that is not the clock or the site frame."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sensor_id: str = Field(min_length=1, max_length=64)
    link: UartLinkConfig
    mount: MountConfig
    uncertainty: UncertaintyConfig = UncertaintyConfig()
    confidence: ConfidenceConfig = ConfidenceConfig()
    gate: DetectionGateConfig = DetectionGateConfig()

    raw_log_path: Path | None = None
    """Where to append timestamped raw frames. When set, **every** byte-level frame is logged
    before it is interpreted, including malformed ones.

    This is not a debugging luxury. Nothing this hardware reports about a small airframe has
    been validated, and the only way to validate it is to hold the raw stream next to what was
    actually flying. Detection logic written before that dataset exists is guesswork."""

    stale_after_s: float = Field(default=5.0, gt=0.0)
    """Silence beyond this marks the adapter DEGRADED in its own health record. It does not
    decide the sensor is broken — whether silence means a fault or an empty sky is a site
    policy question and belongs to `backend/system_health` (FM-010)."""

    framing_error_budget: int = Field(default=32, ge=1)
    """Consecutive discarded bytes tolerated before health reports DEGRADED. A stream that
    never syncs — the signature of a wrong baud rate — trips this rather than looking like an
    empty sky."""
