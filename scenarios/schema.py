"""Scenario definition schema — what the simulator is asked to fly, and what it must not.

A scenario is a versioned JSON document, not code. That is what makes the library in this
directory double as an acceptance suite: `purpose` and `expected_behaviour` state in words
what correct handling looks like, and a later stage turns those statements into assertions
against a replayed run.

Two decisions in here are worth reading before writing a scenario.

**Positions are site-relative ENU metres, not latitude and longitude.** A scenario describes a
geometry — "approaches the protected area from the north-west at 900 metres" — and that
geometry is what makes it reusable across sites. Pinning it to WGS-84 coordinates would bind
every scenario to one site's location and would put the same `cos(latitude)` trap in the
scenario files that ADR 0004 keeps out of the tracking maths. `start.altitude_m` is therefore
metres above the **site reference point**, and `scenarios/world.py` is what converts to the
canonical ellipsoidal datum.

**`seed` is required and has no default.** ADR 0008: a scenario run must be reproducible from
its seed alone, and a defaulted seed is a scenario whose author never thought about it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sensors.adapters.simulated.degradation import SensorDegradation
from sensors.schemas.detection import ClassificationLabel

SCENARIO_SCHEMA_VERSION = "1.0.0"

DEFAULT_MAX_SIMULATED_OBJECTS = 20
"""Ceiling on simultaneous objects in a scenario unless it raises its own.

Scenario I exists to find where the system stops keeping up, so the swarm size has to be a
number somebody can turn up. It is a *ceiling* rather than a bare default because raising it
should be a deliberate edit to the scenario, recorded in the file — saturation is a defined
and observable state (ADR 0008), not something a scenario should be able to wander into.
"""


class Behaviour(StrEnum):
    """What the object is doing, as an annotation.

    Descriptive only. The trajectory comes entirely from the integrated motion parameters and
    never from this field, because two sources of motion would eventually disagree and the
    file would then no longer say what the object does. It is here because scenarios are read
    by people, and because classification and risk will later want a ground-truth intent label
    to score themselves against.
    """

    TRANSIT = "transit"
    APPROACH = "approach"
    LOITER = "loiter"
    ORBIT = "orbit"
    ERRATIC = "erratic"
    DEPARTING = "departing"


class AvailabilityState(StrEnum):
    """What a scripted availability window does to a sensor."""

    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class StartPoint(BaseModel):
    """Where an object begins, in the site's local ENU frame."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    east_m: float
    north_m: float
    altitude_m: float
    """Metres above the **site reference point** — the ENU 'up' coordinate. Not above the
    ellipsoid, not above ground level. The world model adds the site reference altitude when
    it converts to the canonical datum, and getting this wrong is FM-004 in miniature."""


class Motion(BaseModel):
    """Constant-velocity motion with acceleration and turn rate integrated over time.

    Not waypoints. An object that teleports between waypoints has infinite acceleration at
    every corner, which produces a velocity sequence no filter can follow and makes every
    tracking test measure the simulator's discontinuities rather than the tracker.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    speed_mps: float = Field(ge=0.0)
    heading_deg: float = Field(ge=0.0, lt=360.0)
    """Degrees clockwise from true north, matching the canonical schema's convention."""

    acceleration_mps2: float = 0.0
    turn_rate_dps: float = 0.0
    """Positive turns clockwise, consistent with the heading convention."""

    climb_rate_mps: float = 0.0

    max_speed_mps: float | None = Field(default=None, gt=0.0)
    """Upper bound on integrated speed. Acceleration left unbounded over a five-minute
    scenario produces a small rotary-wing aircraft travelling at several hundred metres per
    second, and the scenario then tests nothing anybody intended."""

    min_altitude_m: float = 0.0
    """Floor for the integrated altitude, relative to the site reference. An object descending
    at a constant rate has to stop somewhere, and it should be the ground."""

    @model_validator(mode="after")
    def _speed_is_within_its_own_bound(self) -> Motion:
        if self.max_speed_mps is not None and self.speed_mps > self.max_speed_mps:
            raise ValueError("speed_mps starts above max_speed_mps")
        return self


class SimulatedObject(BaseModel):
    """One object in the air, its motion, and when it is there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    object_id: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=256)

    start: StartPoint
    motion: Motion

    true_classification: ClassificationLabel
    """What the object actually is. Reaches a sensor only through its confusion model — no
    adapter may report this value directly (ADR 0002)."""

    behaviour: Behaviour

    appears_at_s: float = Field(default=0.0, ge=0.0)
    """When the object enters the scenario. Its trajectory is integrated from this moment, so
    `start` is where it is when it appears rather than where it was at t=0."""

    disappears_at_s: float | None = Field(default=None, gt=0.0)
    """When it leaves. None means it is present until the scenario ends."""

    @model_validator(mode="after")
    def _schedule_is_ordered(self) -> SimulatedObject:
        if self.disappears_at_s is not None and self.disappears_at_s <= self.appears_at_s:
            raise ValueError(
                f"object {self.object_id}: disappears_at_s must be after appears_at_s"
            )
        return self


class SensorAvailabilityWindow(BaseModel):
    """A scripted change in one sensor's performance, over an interval.

    Scenario E needs a sensor that degrades partway through a run, and FM-010 needs a sensor
    going quiet to be a tested behaviour rather than an assumption. Both are properties of the
    *run*, not of the sensor, so they live here rather than in the sensor registry.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sensor_id: str = Field(min_length=1, max_length=64)
    state: AvailabilityState
    from_s: float = Field(ge=0.0)
    to_s: float | None = Field(default=None, gt=0.0)
    """None means the window runs to the end of the scenario."""

    detection_probability_scale: float = Field(default=1.0, ge=0.0, le=1.0)
    noise_scale: float = Field(default=1.0, gt=0.0)
    reason: str = Field(min_length=1, max_length=256)
    """Why the sensor is in this state, in words an operator could be shown."""

    @model_validator(mode="after")
    def _window_matches_its_state(self) -> SensorAvailabilityWindow:
        if self.to_s is not None and self.to_s <= self.from_s:
            raise ValueError(f"sensor {self.sensor_id}: to_s must be after from_s")

        scales_are_nominal = self.detection_probability_scale == 1.0 and self.noise_scale == 1.0
        if self.state is AvailabilityState.ONLINE and not scales_are_nominal:
            raise ValueError(
                f"sensor {self.sensor_id}: an online window must leave the scales at 1.0, "
                "otherwise the file says the sensor is healthy while degrading it"
            )
        if self.state is AvailabilityState.DEGRADED and scales_are_nominal:
            raise ValueError(
                f"sensor {self.sensor_id}: a degraded window that changes nothing reads as if "
                "it does something; set a scale or use state 'online'"
            )
        return self

    def covers(self, elapsed_s: float) -> bool:
        return self.from_s <= elapsed_s and (self.to_s is None or elapsed_s < self.to_s)

    def as_degradation(self) -> SensorDegradation:
        """The runtime value a simulated adapter is given while this window is in force."""
        return SensorDegradation(
            offline=self.state is AvailabilityState.OFFLINE,
            detection_probability_scale=self.detection_probability_scale,
            noise_scale=self.noise_scale,
            reason=self.reason,
        )


class Scenario(BaseModel):
    """One named, reusable scenario. Loaded from `scenarios/`, never constructed in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCENARIO_SCHEMA_VERSION
    scenario_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1024)

    purpose: str = Field(min_length=1, max_length=1024)
    """What this scenario is *for* — the question it is meant to ask of the system."""

    expected_behaviour: list[str] = Field(min_length=1)
    """What correct handling looks like, one statement per line: "must not alert", "must
    escalate before the object reaches the protected zone", "must not swap track identity".
    These are the acceptance criteria; a scenario without them is a demonstration, not a
    test."""

    seed: int = Field(ge=0)
    """Required, with no default (ADR 0008). Every random draw the simulator makes descends
    from this and the sensor's ID, so a run is reproducible from the file alone."""

    site_id: str = Field(min_length=1, max_length=64)
    """Which site definition this scenario's geometry is meant for. Resolved by the runner
    against `config/sites/`; a scenario file never carries site geometry of its own."""

    duration_s: float = Field(gt=0.0)
    max_objects: int = Field(default=DEFAULT_MAX_SIMULATED_OBJECTS, ge=1)

    objects: list[SimulatedObject] = Field(default_factory=list)
    sensor_availability: list[SensorAvailabilityWindow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _is_internally_consistent(self) -> Scenario:
        ids = [o.object_id for o in self.objects]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate object_id in scenario {self.scenario_id}: {duplicates}")

        if len(self.objects) > self.max_objects:
            raise ValueError(
                f"scenario {self.scenario_id} declares {len(self.objects)} objects but a "
                f"ceiling of {self.max_objects}: raise max_objects deliberately if the "
                "saturation level is meant to change"
            )

        for obj in self.objects:
            if obj.appears_at_s >= self.duration_s:
                raise ValueError(
                    f"object {obj.object_id} appears at {obj.appears_at_s}s, at or after the "
                    f"scenario ends at {self.duration_s}s"
                )

        for window in self.sensor_availability:
            if window.from_s >= self.duration_s:
                raise ValueError(
                    f"sensor {window.sensor_id}: availability window starts at "
                    f"{window.from_s}s, at or after the scenario ends"
                )
        return self

    def object(self, object_id: str) -> SimulatedObject:
        for obj in self.objects:
            if obj.object_id == object_id:
                return obj
        raise KeyError(f"no object {object_id!r} in scenario {self.scenario_id}")
