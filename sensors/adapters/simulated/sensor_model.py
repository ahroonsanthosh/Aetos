"""Sensor model: the parameters a simulated sensor degrades ground truth through.

**Every number in this module and in `config/sensor_registry/` is a simulation parameter.**
None of it is a measurement of, a specification for, or a claim about any real device. The
profiles shipped in the registry are plausible generic shapes chosen to exercise the pipeline
— in particular to give different sensors genuinely different error geometry, so that fusion
and association are tested against disagreement rather than against four copies of the same
noise.

The model exists because of ADR 0002: the simulator knows the truth exactly, and the only
thing standing between that and the tracker consuming perfect data is a configured, explicit
degradation. Making the degradation a versioned config document rather than constants buried
in the adapter is what makes it reviewable.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sensors.schemas.detection import ClassificationLabel, SensorType

SENSOR_MODEL_SCHEMA_VERSION = "1.0.0"


class Coverage(BaseModel):
    """What the sensor can see, geometrically. Outside this, no detection is produced.

    Elevation bounds are angles above the horizontal *at the sensor*, so a sensor mounted on a
    mast still cannot see an object directly overhead unless its upper bound says it can. This
    is the field that stops a wide-area sensor from implausibly detecting an object hovering
    on top of it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_range_m: float = Field(gt=0.0)
    min_range_m: float = Field(default=0.0, ge=0.0)
    """Blind zone. A real pulsed sensor cannot resolve returns closer than its own pulse
    length, and a scenario that flies an object over the sensor should exercise that."""

    azimuth_extent_deg: float = Field(gt=0.0, le=360.0)
    """Total width of the azimuth sector, centred on the placement's boresight. 360 means full
    azimuthal coverage, in which case boresight is meaningless and is ignored."""

    elevation_min_deg: float = Field(ge=-90.0, le=90.0)
    elevation_max_deg: float = Field(ge=-90.0, le=90.0)

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> Coverage:
        if self.min_range_m >= self.max_range_m:
            raise ValueError("min_range_m must be below max_range_m")
        if self.elevation_min_deg >= self.elevation_max_deg:
            raise ValueError("elevation_min_deg must be below elevation_max_deg")
        return self

    @property
    def has_full_azimuth(self) -> bool:
        return self.azimuth_extent_deg >= 360.0


class Uncertainty(BaseModel):
    """One-sigma measurement error, expressed in the sensor's own frame.

    Anisotropic on purpose, and this is the whole reason `position_covariance_enu` exists in
    the canonical schema. Sensor error is naturally split into an along-line-of-sight (range)
    component and an across-line-of-sight component, and for most sensors the two differ by
    an order of magnitude — in opposite directions for different kinds. A radar measures range
    well and bearing poorly; an imaging sensor measures bearing very well and range badly.
    Collapsing either into a single isotropic number throws away exactly the information that
    keeps two crossing tracks apart.

    Cross-range error has two terms because it physically has two: a fixed floor, and an
    angular term that grows with range, since an angular error subtends more metres the
    further away the object is.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    range_sigma_m: float = Field(ge=0.0)
    cross_range_sigma_m: float = Field(ge=0.0)
    cross_range_sigma_mrad: float = Field(default=0.0, ge=0.0)
    vertical_sigma_m: float = Field(ge=0.0)

    def cross_range_sigma_at_m(self, range_m: float) -> float:
        """Total cross-range one-sigma at a given range, floor and angular terms combined."""
        angular = range_m * self.cross_range_sigma_mrad * 1e-3
        return math.hypot(self.cross_range_sigma_m, angular)


class ConfidenceModel(BaseModel):
    """How the source's own confidence that a return is real varies with range.

    `false_alarm_scale` matters more than it looks. False alarms must be *statistically*
    weaker than real returns but must overlap them, because a clutter return that is
    identifiable by its confidence alone would let a downstream module separate real from
    spurious perfectly — which is precisely the kind of privileged information ADR 0002 exists
    to keep out of the pipeline.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    near_range: float = Field(ge=0.0, le=1.0)
    far_range: float = Field(ge=0.0, le=1.0)
    jitter_sigma: float = Field(default=0.0, ge=0.0)
    false_alarm_scale: float = Field(default=1.0, gt=0.0, le=1.0)


class RadialVelocityModel(BaseModel):
    """Whether the sensor reports range rate, and how well.

    Unlike speed and heading, range rate is a quantity a Doppler-capable sensor genuinely
    measures, which is why the canonical schema carries it as its own field. The simulated
    adapter still marks what it emits as `simulated_truth`, because however much noise is
    applied the number originates in the simulator's own state. See the adapter for why.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reported: bool = False
    sigma_mps: float = Field(default=0.0, ge=0.0)


class ClassificationConfusion(BaseModel):
    """How badly the sensor gets the label wrong, and what it says instead.

    A simulated sensor that reports the true label is not a classifier — it is a leak. The
    classification module would then be a passthrough and its entire test suite would be
    measuring nothing (ADR 0002). This model is the minimum needed to make that impossible:
    the reported label is a draw, not a copy.

    `confusable_with` is where the mass goes when the draw comes out wrong. Listing coarse
    labels such as `unknown_uas` there models the realistic failure — a sensor that can tell
    something is an aircraft but not what size — rather than a random substitution.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correct_probability: float = Field(ge=0.0, le=1.0)
    confusable_with: list[ClassificationLabel] = Field(min_length=1)
    confidence_when_correct: float = Field(ge=0.0, le=1.0)
    confidence_when_confused: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _confusion_is_not_certainty(self) -> ClassificationConfusion:
        if self.correct_probability >= 1.0:
            raise ValueError(
                "correct_probability of 1.0 means the sensor emits ground-truth labels, "
                "which ADR 0002 forbids: detection classification is evidence, not truth"
            )
        return self


class SensorModel(BaseModel):
    """One simulated sensor profile, as loaded from `config/sensor_registry/`.

    Placement — where the sensor sits and which way it faces — is *not* here. It lives on the
    site's `SensorPlacement`, because the same model can be fitted at many sites and one site
    can be fitted with several sensors of the same model. The registry key is the join.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SENSOR_MODEL_SCHEMA_VERSION
    registry_key: str = Field(min_length=1, max_length=64)
    sensor_type: SensorType
    description: str = Field(min_length=1, max_length=512)

    coverage: Coverage
    update_rate_hz: float = Field(gt=0.0, le=1000.0)
    uncertainty: Uncertainty
    confidence: ConfidenceModel

    probability_of_detection: float = Field(ge=0.0, le=1.0)
    """Per-update chance that an object inside coverage is reported at all. The other half of
    honest simulation: a sensor that sees everything it can geometrically see produces track
    continuity no real deployment ever gets."""

    false_alarm_rate_hz: float = Field(default=0.0, ge=0.0)
    """Expected spurious returns per second, across the whole coverage volume. Drawn as a
    Poisson count per update rather than a fixed number, because clutter arrives in clumps."""

    radial_velocity: RadialVelocityModel = Field(default_factory=RadialVelocityModel)
    classification: ClassificationConfusion | None = None
    """None means this sensor does not classify at all, which is the honest model for a plain
    ranging sensor. The detection then carries no classification block."""

    @model_validator(mode="after")
    def _is_a_simulated_type(self) -> SensorModel:
        if not self.sensor_type.is_simulated:
            raise ValueError(
                f"registry entry {self.registry_key!r} declares sensor_type "
                f"{self.sensor_type.value!r}: the simulated adapter must never present itself "
                "as a real sensor, because provenance has to be visible in the data"
            )
        return self

    @property
    def update_period_s(self) -> float:
        return 1.0 / self.update_rate_hz
