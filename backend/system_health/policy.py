"""How long silence has to last before it is worth telling someone about.

Configuration rather than code, on the same grounds as the risk weights and the alerting
thresholds: this is a deployment's judgement, not a property of the software. A 10 Hz imaging
sensor that has said nothing for ten seconds has almost certainly failed; a 1 Hz
radio-frequency sensor watching a rural approach may legitimately say nothing for an hour. A
single number compiled into the watchdog would be wrong for both.

The thresholds here decide *when the fact is worth reporting*. They never decide what the fact
means — that judgement is the operator's, and the assessment carries the number it was made
against so it can be argued with. See `backend/system_health/models.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from backend.common.config import ConfigError, ConfigPaths
from sensors.schemas.detection import SensorType

DEFAULT_POLICY_FILE = "policy.json"


class SystemHealthPolicy(BaseModel):
    """Thresholds governing what the watchdog says about silence and stalls."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    notes: str = ""
    """Free text for whoever edits this file. Declared rather than tolerated, because the
    config models forbid extra keys and a `_comment` would otherwise fail validation."""

    default_silence_threshold_s: float = Field(default=60.0, gt=0.0)
    """How long any source may emit nothing before the watchdog calls it QUIET.

    Applies to sensor types with no entry in `silence_threshold_s`. Generous by default: the
    cost of reporting quiet too eagerly is an operator who learns to ignore the row, which is
    the same failure that governs the alerting thresholds.
    """

    silence_threshold_s: dict[SensorType, float] = Field(default_factory=dict)
    """Per-sensor-type overrides, because the right window differs by an order of magnitude
    across the kinds of sensor this system is designed to accept."""

    recovery_dwell_windows: float = Field(default=1.5, gt=1.0)
    """How many silence windows a source must sustain before a bad verdict is withdrawn.

    The hysteresis, and it is not optional. Silence is a sawtooth: a source producing one
    detection every 35 seconds against a 30-second window crosses the threshold, gets a
    detection, drops back under it, and crosses again — forever. Without a dwell that is a
    verdict flapping twice a minute, a display blinking between OK and QUIET, and roughly two
    hundred status events an hour per sensor burying the one transition that mattered.

    **Expressed in windows rather than seconds, and required to exceed one.** A source flapping
    across its threshold accumulates exactly one window of apparent health between crossings —
    from its last detection to its next crossing — so any dwell of one window or less is
    satisfied by the flap itself and buys nothing. That is not a tuning preference a deployment
    should be free to get wrong, so the bound is on the field. Stating it in windows also makes
    it scale with the sensor type for free: a 20-second imaging window and a 120-second
    radio-frequency one get proportionate dwells from one number.

    Asymmetric on purpose, exactly as `AlertPolicy.clear_dwell_s` is (ADR 0007): a bad verdict
    is reached the instant the evidence supports it and withdrawn only once the source has
    earned it back. A genuinely marginal sensor therefore settles on QUIET and stays there,
    which is the true statement about it — rather than alternating between two verdicts of
    which one is always wrong.
    """

    startup_grace_s: float = Field(default=30.0, ge=0.0)
    """How long after a source starts before its silence is measured at all.

    Without it every restart reports every sensor quiet for one window, and an operator who
    sees that on every start stops reading it. Measured from when the watchdog first saw the
    source, which is what the watchdog knows; an adapter does not report its own start time.
    """

    stall_threshold_s: float = Field(default=10.0, gt=0.0)
    """How stale the last assessment may be before the system is reported STALLED.

    This is the self-test. Every other component in this system reports on itself, and a
    component that has stopped running reports nothing at all — which renders identically to a
    component with nothing to say. Comparing the age of the last pass against this threshold at
    *read* time is the one check that cannot be defeated by the thing it is checking having
    stopped.

    Must comfortably exceed the pipeline tick. `validate_against_tick` is how a deployment
    finds out it does not.
    """

    assess_simulated_sensors: bool = True
    """Whether simulated sources are judged like any other.

    True, deliberately. A scenario that has finished driving its sensors is a legitimate reason
    for silence, and excluding simulated sources would leave the watchdog saying nothing at all
    in the mode most people run — which means nobody would notice it was broken until the day
    it mattered. Set False only for a deployment where synthetic sources are expected to idle.
    """

    def threshold_for(self, sensor_type: SensorType) -> float:
        """The silence window for one kind of source."""
        return self.silence_threshold_s.get(sensor_type, self.default_silence_threshold_s)

    def recovery_dwell_for(self, sensor_type: SensorType) -> float:
        """How long this kind of source must sustain health before a bad verdict is withdrawn.

        Derived from the silence window rather than configured beside it, so the two cannot be
        set into the combination that makes the dwell useless. See `recovery_dwell_windows`.
        """
        return self.threshold_for(sensor_type) * self.recovery_dwell_windows

    def validate_against_tick(self, tick_s: float) -> None:
        """Refuse a stall threshold the pipeline cannot possibly satisfy.

        A `stall_threshold_s` below the tick makes the system declare itself stalled on every
        healthy cycle, which is worse than having no stall detection: it trains the operator
        that the alarm means nothing. Called by the wiring, where both numbers are known.
        """
        if self.stall_threshold_s <= tick_s:
            raise ConfigError(
                f"stall_threshold_s ({self.stall_threshold_s}s) is not above the pipeline tick "
                f"({tick_s}s); the system would report itself stalled on every healthy cycle"
            )


def load_system_health_policy(
    paths: ConfigPaths | None = None, filename: str = DEFAULT_POLICY_FILE
) -> SystemHealthPolicy:
    """Read the watchdog policy from `config/system_health/`.

    A missing file is an error rather than a silent fall back to defaults, for the same reason
    the alerting loader refuses one: defaults that apply invisibly are discovered during an
    incident.
    """
    resolved = paths or ConfigPaths.default()
    file = resolved.config_root / "system_health" / filename

    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{file}: system health policy not found") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{file}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{file}: could not be read: {exc}") from exc

    try:
        return SystemHealthPolicy.model_validate(document)
    except Exception as exc:  # pydantic ValidationError, reported with the file that caused it
        raise ConfigError(f"{file}: invalid system health policy: {exc}") from exc


def policy_path(paths: ConfigPaths | None = None) -> Path:
    """Where the policy is read from. Exposed for diagnostics and tests."""
    resolved = paths or ConfigPaths.default()
    return resolved.config_root / "system_health" / DEFAULT_POLICY_FILE
