"""Canonical Detection schema — the contract every sensor source must satisfy.

This is the narrow waist of the entire system. The simulator and every future real sensor
adapter produce exactly this shape, and nothing downstream of ingestion can tell which
produced a given detection.

See docs/detection_schema.md for the field-by-field contract and docs/adr/0002 for why
`kinematics` and `classification` are optional and provenance-marked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Semantic version of this schema. Bump minor for backwards-compatible additions, major for
# breaking changes. Every Detection carries the version it was produced under so that a
# future adapter written against an older version remains identifiable.
DETECTION_SCHEMA_VERSION = "1.0.0"


class SensorType(StrEnum):
    """What kind of source produced a detection.

    Deliberately generic. No manufacturer, model or product name appears here or anywhere
    else in the codebase — the system must never depend on a particular vendor.
    """

    SIMULATED_RADAR = "simulated_radar"
    SIMULATED_EO_IR = "simulated_eo_ir"
    SIMULATED_RF = "simulated_rf"
    SIMULATED_ACOUSTIC = "simulated_acoustic"

    # Real sensor sources. `RADAR` is now produced by `sensors/adapters/hardware_radar`,
    # driving the testbed unit described in docs/radar_testbed.md — a placeholder for the
    # eventual production radar, uncharacterised against small airframes, and not yet a basis
    # for trusted detection. The remaining three are schema reservations, not capability
    # claims: no implementation exists for any of them.
    RADAR = "radar"
    EO_IR = "eo_ir"
    RF = "rf"
    ACOUSTIC = "acoustic"

    @property
    def is_simulated(self) -> bool:
        return self.value.startswith("simulated_")


class Provenance(StrEnum):
    """How a derived field came to hold its value.

    This exists so that the difference between "the sensor measured this" and "the simulator
    knows this because it created the object" is explicit in the data rather than implied by
    which code path produced it. See docs/adr/0002.
    """

    MEASURED = "measured"
    """The sensor directly measured this quantity."""

    DERIVED = "derived"
    """The adapter computed it from that sensor's own measurements."""

    SIMULATED_TRUTH = "simulated_truth"
    """Simulator ground truth. Never authoritative. Present so that tests can assert the
    tracker does not depend on it, and so operators can never be shown it as fact."""


class ClassificationLabel(StrEnum):
    """The closed set of labels any source or the classification module may assert."""

    UNKNOWN = "unknown"
    UNKNOWN_UAS = "unknown_uas"
    SMALL_UAS = "small_uas"
    MEDIUM_UAS = "medium_uas"
    LARGE_UAS = "large_uas"
    BIRD = "bird"
    VEHICLE = "vehicle"
    OTHER = "other"


class SensorRef(BaseModel):
    """Which source produced this detection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sensor_id: str = Field(min_length=1, max_length=64)
    sensor_type: SensorType


class GeoPosition(BaseModel):
    """A position in WGS-84.

    Altitude is metres above the **WGS-84 ellipsoid** — not above ground level, not above
    mean sea level, not barometric. Confusing these is a silent error of tens to hundreds of
    metres, so any future real adapter is responsible for converting to this datum and that
    obligation is part of the adapter contract. See docs/failure_modes.md FM-004.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    altitude_m: float


class Kinematics(BaseModel):
    """Optional motion estimate reported by the source.

    Most sensors do not measure these. A radar measures range, azimuth, elevation and
    sometimes *radial* Doppler; it does not measure heading. Speed and heading are normally
    quantities a tracker derives from a sequence of positions.

    The V0 tracker therefore does **not** consume this block for state estimation, whatever
    its provenance. It is carried because some sources genuinely produce it and downstream
    consumers other than the filter (diagnostics, fusion tie-breaking evidence) may use it.

    Enforced by tests/integration/test_detection_provenance_guard.py. See docs/adr/0002.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    speed_mps: float | None = Field(default=None, ge=0.0)
    heading_deg: float | None = Field(default=None, ge=0.0, lt=360.0)
    vertical_rate_mps: float | None = None
    radial_velocity_mps: float | None = None
    """Range-rate toward (negative) or away from (positive) the sensor. Unlike speed and
    heading this *is* directly measurable by a Doppler-capable sensor. Carried separately so
    that a future ADR can admit it into the filter as a genuine measurement without also
    admitting the derived fields."""

    provenance: Provenance = Provenance.DERIVED


class Measurement(BaseModel):
    """How uncertain the reported position is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position_uncertainty_m: float = Field(ge=0.0)
    """One-sigma positional uncertainty, isotropic. Required by schema v1.0."""

    position_covariance_enu: list[list[float]] | None = None
    """Optional 3x3 covariance in local ENU metres squared, row-major [E, N, U].

    Real sensor uncertainty is usually strongly anisotropic — a radar is precise in range
    and imprecise in cross-range — and a single scalar cannot express that. Sources able to
    report a full covariance should; the filter uses it in preference to the scalar when
    present, and falls back to an isotropic matrix built from the scalar when it is not."""

    @field_validator("position_covariance_enu")
    @classmethod
    def _validate_covariance_shape(
        cls, v: list[list[float]] | None
    ) -> list[list[float]] | None:
        if v is None:
            return v
        if len(v) != 3 or any(len(row) != 3 for row in v):
            raise ValueError("position_covariance_enu must be a 3x3 matrix")
        for i in range(3):
            for j in range(i + 1, 3):
                if abs(v[i][j] - v[j][i]) > 1e-9 * max(1.0, abs(v[i][j])):
                    raise ValueError("position_covariance_enu must be symmetric")
        if any(v[i][i] < 0.0 for i in range(3)):
            raise ValueError("position_covariance_enu must have non-negative variances")
        return v


class ClassificationHint(BaseModel):
    """What the source believes it is looking at.

    This is **evidence, not a verdict**. Some sources genuinely classify — an EO/IR sensor
    with a vision model attached does real work here. The classification module weights this
    as one input among several, discounted by provenance, and is required to produce a
    defensible label without it. See docs/adr/0002.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: ClassificationLabel
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: Provenance = Provenance.MEASURED


class Quality(BaseModel):
    """How much the source trusts that this detection corresponds to a real object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    confidence: float = Field(ge=0.0, le=1.0)
    """Source's confidence that this is a real object rather than noise or clutter."""

    snr_db: float | None = None


class Detection(BaseModel):
    """One observation, by one source, at one instant.

    A Detection is a statement that a sensor observed something at a location at a time. It
    is not a belief about an object's identity or continuity — that is a Track, and forming
    Tracks from Detections is the tracking engine's job.

    Immutable once constructed: a detection is a historical fact and correcting it means
    emitting a new one, never mutating the original.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = DETECTION_SCHEMA_VERSION
    detection_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    """When the observation was made, UTC. Not when it was received or processed."""

    sensor: SensorRef
    position: GeoPosition
    measurement: Measurement
    quality: Quality

    kinematics: Kinematics | None = None
    classification: ClassificationHint | None = None

    @field_validator("timestamp")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        """Reject naive datetimes and normalise to UTC.

        A naive timestamp is an ambiguous timestamp, and ambiguous timestamps in a
        multi-sensor system produce velocity estimates that are wrong by whatever the
        timezone offset happens to be. Rejecting them at the schema boundary is cheaper than
        diagnosing it later. See docs/failure_modes.md FM-012.
        """
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware; naive datetimes are ambiguous")
        return v.astimezone(UTC)

    def without_inferred_fields(self) -> Detection:
        """Return a copy with `kinematics` and `classification` removed.

        This is the instrument the provenance guard test uses: a scenario is run once with
        detections as produced and once with every detection passed through this method, and
        the resulting track states must be identical. If they differ, the tracking engine has
        acquired a dependency on information that real sensors may not supply.

        See docs/adr/0002 and docs/failure_modes.md FM-021.
        """
        return self.model_copy(update={"kinematics": None, "classification": None})
