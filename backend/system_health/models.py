"""What the watchdog concludes, and the vocabulary it is allowed to conclude it in.

The distinction this file exists to hold is the whole of FM-010: **loss of contact with a
sensor is a fact; absence of detections is not.** A driver that cannot open its port is
definitely broken. A radar that has reported nothing for ninety seconds may be broken, or may
be pointed at an empty sky, and no amount of looking at the sensor will tell you which.

So the verdict set separates the two. `OFFLINE` and `FAULTED` are assertions about the sensor.
`QUIET` is an assertion about the *data* — "this source is in contact and has emitted nothing
for longer than this site said to expect" — and it deliberately does not claim the sensor is
broken. The operator is told the fact and the threshold that made it worth saying, and decides.

Collapsing `QUIET` into a fault would cry wolf at every calm night. Collapsing it into `OK`
would be the failure this module was written against: a green light over a dead sensor.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from sensors.schemas.detection import SensorType


class SensorVerdict(StrEnum):
    """The watchdog's judgement about one source.

    Ordered by severity in `severity`, so a posture can be derived without a chain of
    comparisons and so two verdicts can be ranked without every caller re-encoding the order.
    """

    NOT_STARTED = "not_started"
    """Attached but never started. Not a fault — nothing has been asked of it yet."""

    OK = "ok"
    """In contact and producing detections within the interval this site expects."""

    QUIET = "quiet"
    """In contact, healthy by its own account, and has emitted nothing for longer than the
    policy window.

    **Not a fault claim.** An empty sky produces exactly this. It is reported because the
    alternative — saying nothing — is what makes a dead sensor and a calm night render
    identically on the operator's display (FM-010).
    """

    DEGRADED = "degraded"
    """Running with reduced capability the adapter itself named, or failing its polls."""

    OFFLINE = "offline"
    """Not observing at all. Contact is lost, and this is a definite fault."""

    @property
    def severity(self) -> int:
        return _VERDICT_SEVERITY[self]

    @property
    def is_observing(self) -> bool:
        """Whether this source is still contributing to the picture.

        `QUIET` counts as observing: a sensor that reports nothing because there is nothing
        there is doing its job. `DEGRADED` counts too — reduced capability is still capability,
        and treating it as blindness would make the system declare itself blind over a single
        sensor running at half rate.
        """
        return self in {SensorVerdict.OK, SensorVerdict.QUIET, SensorVerdict.DEGRADED}


_VERDICT_SEVERITY = {
    SensorVerdict.OK: 0,
    SensorVerdict.QUIET: 1,
    SensorVerdict.NOT_STARTED: 2,
    SensorVerdict.DEGRADED: 3,
    SensorVerdict.OFFLINE: 4,
}
"""`NOT_STARTED` sits above `QUIET` and below `DEGRADED` on purpose: it is not a fault, but a
site that expected four sensors and started three has a real problem the display must not rank
below a quiet one."""


class Posture(StrEnum):
    """What the system as a whole can currently claim about the sky.

    Three states rather than two, because "some sensors are broken" and "no sensor is watching"
    demand different things of an operator, and a single `degraded` flag would present a site
    running on three of four radars the same way as one running on none.
    """

    NOMINAL = "nominal"
    DEGRADED = "degraded"
    """At least one source is faulted, and at least one is still observing. The picture is
    thinner than the site was built for; it is still a picture."""

    BLIND = "blind"
    """Nothing is observing. Whatever is on the display is memory, not observation, and an
    empty display means nothing at all."""

    STALLED = "stalled"
    """The pipeline itself has stopped stepping. Distinct from BLIND: the sensors may be
    perfectly healthy, and nobody is asking them anything. This is the state that no component
    reporting on itself can detect, which is why it is derived from the age of the last
    assessment rather than from anything inside a cycle."""

    @property
    def is_healthy(self) -> bool:
        return self is Posture.NOMINAL


class SensorAssessment(BaseModel):
    """One source's verdict, and the evidence it was reached from.

    The evidence travels with the verdict rather than being discarded. An operator asking why
    the display says a sensor is quiet needs the number of seconds and the threshold that was
    compared against it, not the word.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sensor_id: str = Field(min_length=1, max_length=64)
    sensor_type: SensorType
    verdict: SensorVerdict
    reason: str = Field(min_length=1, max_length=512)
    """Always populated, including for OK — the same rule as `SensorHealth.reason`, and for
    the same reason: a record that explains itself only when something is wrong forces the
    interface to invent the wording for the healthy case."""

    seconds_since_detection: float | None = Field(default=None, ge=0.0)
    """Since this source last emitted anything. None when it has emitted nothing at all since
    it was started, which is a different statement from "a long time ago"."""

    silence_threshold_s: float = Field(ge=0.0)
    """What `seconds_since_detection` was compared against. Carried so the verdict can be
    audited without reading the policy file."""


class HealthAssessment(BaseModel):
    """One pass of the watchdog: every source judged, and the posture that follows.

    Immutable and timestamped. The watchdog holds the most recent one; the API reads it and
    reports how old it is, which is what turns a stalled pipeline into a visible state rather
    than a display that quietly stops changing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    assessed_at: datetime
    posture: Posture
    summary: str = Field(min_length=1, max_length=512)
    sensors: list[SensorAssessment] = Field(default_factory=list)

    @property
    def observing(self) -> list[SensorAssessment]:
        return [s for s in self.sensors if s.verdict.is_observing]
