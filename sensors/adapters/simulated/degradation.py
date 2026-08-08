"""Scripted sensor degradation.

Spec §7 scenario E requires a sensor whose performance falls off partway through a run, and
FM-010 requires that a sensor going quiet be a tested behaviour rather than an assumption.
Both need degradation to be something a scenario can *script*, so it is a value applied to a
running adapter rather than a second set of model parameters.

Kept separate from `SensorModel` because the two answer different questions. The model is what
a sensor is; degradation is what has happened to it. Folding the second into the first would
mean a scenario editing a registry entry, and the registry is shared across sites.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SensorDegradation(BaseModel):
    """How much worse than its model a sensor is currently performing.

    Multipliers rather than replacement values, so a degradation is meaningful without knowing
    which sensor it is applied to — "half the detections and twice the noise" scripts the same
    way against a radio-frequency sensor as against an imaging one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    offline: bool = False
    """Producing nothing at all. Distinct from a very low detection probability: an offline
    sensor emits no false alarms either, because there is no receiver running to generate
    them."""

    detection_probability_scale: float = Field(default=1.0, ge=0.0, le=1.0)
    noise_scale: float = Field(default=1.0, gt=0.0)
    """Applied to every measurement sigma. The emitted covariance is scaled with it, because
    an adapter whose noise grows while its reported uncertainty does not is understating
    uncertainty, which is the failure the adapter contract names explicitly."""

    reason: str = Field(default="", max_length=256)
    """Why, in words an operator can read. Empty only for the nominal case."""

    @property
    def is_nominal(self) -> bool:
        return (
            not self.offline
            and self.detection_probability_scale == 1.0
            and self.noise_scale == 1.0
        )


NOMINAL = SensorDegradation()
"""A sensor performing exactly as its model says. The default state of every adapter."""
