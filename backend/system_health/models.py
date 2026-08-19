"""What the watchdog reports.

The vocabulary here is chosen to keep one distinction visible that the system genuinely cannot
resolve: **a sensor producing nothing may be broken, or may be looking at an empty sky.** No
amount of watching one sensor separates those. Reporting the ambiguity is the honest thing an
operator can act on; resolving it silently in either direction is not (FM-010).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from sensors.schemas.detection import SensorType

HEALTH_SCHEMA_VERSION = "1.0.0"


class SensorCondition(StrEnum):
    """What the watchdog concludes about one sensor.

    Wider than `SensorState`, which is the adapter's report about itself. The adapter knows
    whether its link is up; only the watchdog can compare one sensor against the rest and
    against the clock, which is what the last two values below take.
    """

    NOT_STARTED = "not_started"
    """Constructed but never started. Nothing has gone wrong yet."""

    OK = "ok"
    """Running and producing at the rate its configuration expects."""

    DEGRADED = "degraded"
    """The adapter reports reduced capability it can name."""

    SILENT = "silent"
    """Running, link up, and producing nothing for longer than expected — **while other sensors
    are producing**.

    That last clause is the whole discrimination. A sensor alone in a quiet sky is
    indistinguishable from a failed one, and flagging every quiet minute would train an operator
    to ignore the indicator. A sensor producing nothing while its neighbours produce steadily is
    anomalous in a way one sensor's own silence never is."""

    OFFLINE = "offline"
    """Stopped, or unable to observe at all. The adapter says so itself."""

    @property
    def is_contributing(self) -> bool:
        """Whether this sensor is currently known to be adding to the picture."""
        return self in (SensorCondition.OK, SensorCondition.DEGRADED)


class SensorObservation(BaseModel):
    """The watchdog's finding for one sensor, with the facts it rests on.

    Facts alongside the conclusion, deliberately. A condition an operator cannot interrogate is
    one they have to take on trust, and the numbers here are what let them disagree with it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sensor_id: str = Field(min_length=1, max_length=64)
    sensor_type: SensorType
    condition: SensorCondition
    reason: str = Field(min_length=1, max_length=400)
    """Always populated, including when the condition is OK. A record that only explains itself
    when something is wrong forces the interface to invent the wording for the healthy case, and
    the two then drift apart."""

    last_detection_time: datetime | None = None
    detections_emitted: int = Field(default=0, ge=0)

    silent_for_s: float | None = None
    """How long since this sensor last produced anything, or None if it never has and was never
    expected to yet."""

    expected_interval_s: float | None = None
    """The update period its configuration implies, against which silence is judged."""


class SystemHealth(BaseModel):
    """The whole site's sensing posture for one cycle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = HEALTH_SCHEMA_VERSION

    assessed: bool = False
    """Whether the watchdog has actually run. False is not "healthy" — it is "nobody has
    looked", and the two must never render the same way (ADR 0011)."""

    degraded: bool = False
    """Whether the site has too little sensing left to be relied on."""

    reason: str = ""
    """Why, in words, in both directions. An operator told the system is degraded needs to know
    what it lost; one told it is not needs to know that was checked."""

    sensors: list[SensorObservation] = Field(default_factory=list)

    contributing_sensors: int = Field(default=0, ge=0)
    configured_sensors: int = Field(default=0, ge=0)

    @property
    def anomalous(self) -> list[SensorObservation]:
        """Sensors that are not contributing, worst first."""
        order = {
            SensorCondition.OFFLINE: 0,
            SensorCondition.SILENT: 1,
            SensorCondition.NOT_STARTED: 2,
            SensorCondition.DEGRADED: 3,
            SensorCondition.OK: 4,
        }
        return sorted(
            (s for s in self.sensors if not s.condition.is_contributing),
            key=lambda s: (order[s.condition], s.sensor_id),
        )
