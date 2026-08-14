"""Configuration for a hardware radar unit: serial link, detection tuning, mount, uncertainty.

Every number a hardware radar integration wants to argue about lives here rather than in the
driver, for the reason the sensor registry exists for the simulator: parameters buried in code
are not reviewable, and this particular set is going to be swept repeatedly before anybody
trusts a detection from it.

Three groups deserve their own note.

**Link parameters are unverified until measured.** The documented default baud rate for the
unit on the bench is contradicted between sources (115200 in one, 256000 in another), so
`baud_rate` is configuration with a flagged default and `probe_baud_rates` exists to try the
candidates in order. Hardcoding either value would produce a radar that appears connected,
emits a byte stream that never frames, and reports zero targets forever — the exact failure
mode that is hardest to tell from a quiet sky.

**Detection tuning is exposed because the hardware is mis-tuned for the target.** The unit's
firmware is written for vehicle-scale radar cross-sections. Its behaviour against a 135 g,
25 cm airframe is not merely uncertain, it is unmeasured. `sensitivity`, `min_confirm_frames`
and `max_distance_m` are therefore knobs on the outside of the box, not constants inside it.

**Mount tilt is a first-class configuration value, not a note in a build log.** The unit has no
elevation channel: its beam is a fixed cone, and the only way to look upward is to physically
tilt the board. `mount.tilt_deg` is what lets everything downstream know which slice of sky was
actually being scanned, and it is required to be recorded with every test session because a
detection result without it is uninterpretable.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.common.config import ConfigError, load_config

RADAR_CONFIG_SCHEMA_VERSION = "1.0.0"

DEFAULT_RADAR_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "radar"
"""Where radar configuration documents live. One document per physical unit."""


class SerialLinkConfig(BaseModel):
    """The USB-to-TTL serial link to the unit.

    The adapter in use is switchable between 3.3 V and 5 V logic and must be on 3.3 V to match
    the radar's UART, while the radar's own supply pin still needs 5 V. That is a wiring fact,
    not a software setting, and it is stated in `docs/radar_testbed.md` — but it is the single
    most likely cause of a link that opens cleanly and then decodes nothing, so it is worth
    reading before adjusting anything in this block.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: str = Field(min_length=1, max_length=256)
    """Serial device path — `/dev/ttyUSB0` on Linux, `COM3` on Windows."""

    baud_rate: int = Field(default=115_200, gt=0)
    """**Unverified against the physical unit.** Sources disagree; confirm before trusting.
    Whichever value is confirmed, record it here rather than leaving the default in place."""

    probe_baud_rates: list[int] = Field(default_factory=lambda: [115_200, 256_000])
    """Candidate rates `connect()` tries in order when `probe_on_connect` is set.

    Probing is a first-run convenience for exactly the ambiguity above. It is not a substitute
    for confirming the rate: a probe that lands on a rate by finding one clean frame can still
    be wrong, so a successful probe logs the rate it settled on and expects it to be written
    back into this document."""

    probe_on_connect: bool = False
    """Off by default. A production link with a known baud rate must not spend its first
    seconds cycling through rates, and a link that needs probing every time it starts is a link
    whose configuration is wrong."""

    read_timeout_s: float = Field(default=0.1, gt=0.0, le=5.0)
    """Blocking read budget per poll. Small: `get_targets()` is called from the pipeline loop
    and must return promptly whether or not the radar had anything to say."""

    max_read_bytes: int = Field(default=4096, gt=0)
    """Ceiling on bytes consumed per poll, so a backed-up buffer cannot stall the loop."""

    reconnect_initial_delay_s: float = Field(default=1.0, gt=0.0)
    reconnect_max_delay_s: float = Field(default=30.0, gt=0.0)
    """Backoff bounds for automatic reconnection after the link drops. Bounded rather than
    unbounded: a radar that has been unplugged for an hour must still come back within half a
    minute of being plugged in, without anybody restarting the process."""

    max_reconnect_attempts: int | None = Field(default=None, ge=1)
    """Give up after this many consecutive failures, or None to retry indefinitely. Giving up
    moves the driver to FAULTED, which is louder than RECONNECTING — appropriate when the port
    has been removed from the machine rather than merely gone quiet."""

    @model_validator(mode="after")
    def _backoff_bounds_are_ordered(self) -> SerialLinkConfig:
        if self.reconnect_initial_delay_s > self.reconnect_max_delay_s:
            raise ValueError(
                f"reconnect_initial_delay_s ({self.reconnect_initial_delay_s}) must not exceed "
                f"reconnect_max_delay_s ({self.reconnect_max_delay_s})"
            )
        if self.probe_on_connect and not self.probe_baud_rates:
            raise ValueError("probe_on_connect is set but probe_baud_rates is empty")
        return self


class DetectionTuningConfig(BaseModel):
    """Settings pushed into the unit's own firmware.

    Named for what they do rather than for the register they land in, so that a different unit
    with the same three concepts can reuse the block and a unit without one of them can reject
    it explicitly in `configure()`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_distance_m: int = Field(default=100, ge=10, le=100)
    """Detection distance the unit is told to use. Bounded by what the hardware accepts, not by
    what it can achieve: the 100 m figure is rated against vehicle-scale returns and says
    nothing about a small airframe."""

    min_speed_kmh: int = Field(default=0, ge=0, le=120)
    """Reports below this radial speed are suppressed by the firmware. Default 0 — suppression
    is a tuning decision to be made from test data, and a hovering airframe has a radial speed
    of zero."""

    sensitivity: int = Field(default=4, ge=0, le=8)
    """Detection sensitivity, lower being more sensitive. Swept, not set: the whole point of
    exposing it is that the correct value against the intended target is unknown."""

    min_confirm_frames: int = Field(default=1, ge=1, le=10)
    """Consecutive frames a target must appear in before the unit reports it.

    Raising it suppresses clutter at the cost of latency and of dropping genuinely intermittent
    returns — and a small airframe at range is exactly the target most likely to be
    intermittent. Default 1, so that the first test campaign sees everything the hardware is
    willing to report and the filtering decision is made from data."""

    no_target_delay_s: int = Field(default=1, ge=0, le=255)
    """How long the unit holds an alarm state after targets disappear."""


class MountConfig(BaseModel):
    """How the unit is physically installed. Geometry, not preference.

    The unit reports angles in its own beam plane and has no idea how it is bolted down. Every
    number here is what turns "12 m at +7 degrees" into a position in the world, and getting one
    wrong is a silent error of tens of metres rather than a visible failure (FM-004).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boresight_deg: float = Field(default=0.0, ge=0.0, lt=360.0)
    """Compass bearing the beam centre faces, degrees clockwise from true north."""

    tilt_deg: float = Field(default=0.0, ge=-90.0, le=90.0)
    """Upward tilt of the whole board from horizontal. Positive is nose-up.

    This is the *only* way this unit achieves elevation coverage, and it buys upward coverage
    by giving up ground-level coverage — one fixed tilt scans one fixed slice of sky. It is
    recorded per test session for the same reason range and angle are: a detection result
    without the tilt that produced it cannot be compared with any other result."""

    height_m: float = Field(default=0.0)
    """Height of the unit above the site reference altitude."""

    azimuth_extent_deg: float = Field(default=40.0, gt=0.0, le=360.0)
    """Total width of the beam in azimuth, centred on boresight. Exposed as configuration
    rather than assumed omnidirectional: this unit sees a narrow cone, and any downstream
    coverage drawing that assumed otherwise would paint confidence over sky nobody is
    watching."""

    elevation_extent_deg: float = Field(default=30.0, gt=0.0, le=180.0)
    """Total vertical width of the beam. The unit cannot say where in this span a target sits,
    so this is not a coverage note — it is the direct source of the vertical uncertainty the
    adapter reports. A wider beam means a less certain altitude, and saying so is the whole
    reason the value is here."""


class UncertaintyConfig(BaseModel):
    """One-sigma measurement error, in the sensor's own frame.

    Deliberately not derived from a datasheet. The unit publishes no error model, and inventing
    a tight one would make the tracker overconfident in returns that have not been characterised
    at all — worse than reporting nothing (ADR 0002). These defaults are conservative
    placeholders to be replaced by numbers measured during the campaign in
    `docs/radar_testbed.md`, and the document that carries them says so.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    range_sigma_m: float = Field(default=1.0, ge=0.0)
    """Along-beam error. The quantity a ranging radar measures best."""

    azimuth_sigma_deg: float = Field(default=3.0, ge=0.0)
    """Angular error about boresight. Becomes metres of cross-range error in proportion to
    range, which is why it is stored as an angle."""

    velocity_sigma_mps: float = Field(default=0.5, ge=0.0)

    reported_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    """Confidence stamped on every canonical Detection built from this unit.

    A flat, configured number, and honest about being one. The hardware exposes no calibrated
    probability, and no measured detection statistics exist for the intended target yet — so
    there is nothing to compute a per-detection confidence from. The alternative, deriving one
    from the unit's uncalibrated signal-strength index, would look like a measurement and be a
    fabrication. This value is expected to be replaced by something range- and
    cross-section-dependent once the test matrix has been run, and not before."""


class RawLogConfig(BaseModel):
    """Raw frame logging. Required before any detection logic built on this unit is trusted.

    Not a debug afterthought: the protocol byte values for this unit come from secondhand
    sources, so the first question about any surprising behaviour is "what did the wire
    actually carry", and that question is unanswerable retrospectively unless the bytes were
    kept.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    path: str | None = None
    """File the raw log appends to, relative to the repository root unless absolute. Required
    when `enabled`."""

    include_discarded: bool = True
    """Log bytes dropped during resynchronisation as well as complete frames. On by default,
    because a stream that never frames produces no frames to log and the discarded bytes are
    then the only evidence of what went wrong."""

    @model_validator(mode="after")
    def _path_present_when_enabled(self) -> RawLogConfig:
        if self.enabled and not self.path:
            raise ValueError("raw_log.enabled is set but no path was given")
        return self


class RadarConfig(BaseModel):
    """Complete configuration for one physical radar unit.

    `sensor_id` and `model` are the identity this unit presents upward. `model` is a free-text
    designation carried into `TargetDetection.raw_source` and the raw log; it is data, never a
    branch — no code anywhere reads it to decide what to do, which is what keeps a hardware
    swap a configuration and driver change rather than a code change.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = RADAR_CONFIG_SCHEMA_VERSION
    sensor_id: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=512)

    link: SerialLinkConfig
    tuning: DetectionTuningConfig = Field(default_factory=DetectionTuningConfig)
    mount: MountConfig = Field(default_factory=MountConfig)
    uncertainty: UncertaintyConfig = Field(default_factory=UncertaintyConfig)
    raw_log: RawLogConfig = Field(default_factory=RawLogConfig)

    min_range_m: float = Field(default=0.5, ge=0.0)
    """Returns closer than this are discarded as near-field artefacts."""

    max_range_m: float = Field(default=100.0, gt=0.0)
    """Returns beyond this are discarded regardless of what the firmware reports. Separate from
    `tuning.max_distance_m`, which is what the unit is *told*; this is what we are willing to
    believe."""

    @model_validator(mode="after")
    def _ranges_are_ordered(self) -> RadarConfig:
        if self.min_range_m >= self.max_range_m:
            raise ValueError(
                f"min_range_m ({self.min_range_m}) must be below max_range_m ({self.max_range_m})"
            )
        return self


def radar_config_path(name: str, directory: Path | None = None) -> Path:
    """Where the document for one unit lives. One file per unit, named by it."""
    return (directory or DEFAULT_RADAR_CONFIG_PATH) / f"{name}.json"


def load_radar_config(name: str, directory: Path | None = None) -> RadarConfig:
    """Load one unit's configuration by document name.

    The document's `sensor_id` must match its filename, for the reason environments and registry
    entries carry the same check: a file copied and edited but not renamed is indistinguishable
    from the unit it claims to be, and that mistake puts one radar's mount tilt on another
    radar's returns.
    """
    path = radar_config_path(name, directory)
    if not path.is_file():
        available = ", ".join(available_radar_configs(directory)) or "none found"
        raise ConfigError(f"{path}: no such radar configuration {name!r}; available: {available}")

    config = load_config(RadarConfig, path)
    if config.sensor_id != name:
        raise ConfigError(
            f"{path}: sensor_id: declares {config.sensor_id!r} but was loaded as {name!r}; "
            f"the file name and the declared sensor_id must agree"
        )
    return config


def available_radar_configs(directory: Path | None = None) -> list[str]:
    """Names of the radar configuration documents in this tree, sorted."""
    directory = directory or DEFAULT_RADAR_CONFIG_PATH
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))
