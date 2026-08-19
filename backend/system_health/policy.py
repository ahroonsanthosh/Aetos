"""Watchdog tuning: how long a sensor may say nothing before that is a finding.

## Why this is configuration and not arithmetic

The first version of this module derived the threshold from each sensor's update rate — eight
missed update periods and the sensor was called silent. That is wrong, and measurably so.

**Update rate is how often the adapter polls. It is not how often a target is in coverage.** A
10 Hz narrow-field EO/IR sensor polls every 100 ms and sees the target only while it is in its
field of view; measured across all nine shipped scenarios, with no fault present anywhere, that
sensor's longest legitimate quiet spell was **81 seconds**, against a derived threshold of 5.
The radars, wide and long-ranged, never exceeded 10.5 s. Deriving from the poll rate flagged
the healthiest sensor on the site continuously and would have taught an operator to ignore the
one indicator built to warn them.

There is no single number that is right for every sensor, because quiet time is dominated by
coverage rather than by health. So the threshold is declared, per FM-010's own wording — *"a
watchdog detects sensor silence past a configured threshold"* — with the resolution order
below, and a sensor's poll rate is reported alongside its health as context rather than used as
a substitute for knowing it.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from backend.common.config import ConfigError, ConfigPaths

DEFAULT_POLICY_FILE = "policy.json"


class HealthPolicy(BaseModel):
    """When silence becomes a finding, and when the site becomes degraded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    notes: str = ""
    """Free text for whoever maintains the file, declared rather than tolerated as an unknown
    key so `extra="forbid"` turns a typo into a startup error."""

    default_silence_s: float = Field(default=120.0, gt=0.0)
    """How long any sensor may produce nothing before it is a finding, absent something more
    specific below.

    Deliberately generous. The cost of being late to notice a dead sensor is bounded — its
    tracks coast, go stale and are lost, all of which are visible on their own. The cost of
    being early is an indicator that fires during ordinary operation, which is how an operator
    learns to stop reading it."""

    silence_by_sensor_type: dict[str, float] = Field(default_factory=dict)
    """Per sensor *type*, keyed by the `SensorType` value. Generalises to any sensor of that
    kind, including ones added later — which a per-identifier list does not."""

    silence_by_sensor_id: dict[str, float] = Field(default_factory=dict)
    """Per individual sensor. The most specific setting, for a unit whose siting makes it
    quieter than others of its type — one pointed at a hillside, say."""

    minimum_contributing_sensors: int = Field(default=1, ge=0)
    """Contributing sensors below which the site is degraded.

    Contributing means producing, or reporting reduced capability it can still name — not
    merely configured. A site whose sensors are all present and all silent has no picture,
    however healthy the inventory looks."""

    def model_post_init(self, _context: object) -> None:
        for label, table in (
            ("silence_by_sensor_type", self.silence_by_sensor_type),
            ("silence_by_sensor_id", self.silence_by_sensor_id),
        ):
            for key, seconds in table.items():
                if seconds <= 0.0:
                    raise ValueError(
                        f"{label}[{key!r}] is {seconds}; a silence threshold of zero or less "
                        "would report every sensor as silent on every cycle"
                    )

    def silence_threshold_s(self, sensor_id: str, sensor_type: str) -> float:
        """How long this sensor may produce nothing before it is a finding.

        Most specific wins: the individual unit, then its type, then the site default.
        """
        if sensor_id in self.silence_by_sensor_id:
            return self.silence_by_sensor_id[sensor_id]
        if sensor_type in self.silence_by_sensor_type:
            return self.silence_by_sensor_type[sensor_type]
        return self.default_silence_s


def load_health_policy(
    paths: ConfigPaths | None = None, filename: str = DEFAULT_POLICY_FILE
) -> HealthPolicy:
    """Read the watchdog policy from `config/system_health/`."""
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
        return HealthPolicy.model_validate(document)
    except Exception as exc:  # pydantic ValidationError, reported with the file that caused it
        raise ConfigError(f"{file}: invalid system health policy: {exc}") from exc
