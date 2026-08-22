"""Canonical Detection schema: validation, immutability, and the provenance contract."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sensors.schemas.detection import (
    DETECTION_SCHEMA_VERSION,
    ClassificationHint,
    ClassificationLabel,
    Detection,
    GeoPosition,
    Kinematics,
    Measurement,
    Provenance,
    Quality,
    SensorRef,
    SensorType,
)

TIMESTAMP = datetime(2026, 8, 8, 12, 0, 1, 250_000, tzinfo=UTC)


def make_detection(**overrides) -> Detection:
    base = {
        "detection_id": "det-simradar01-000123",
        "timestamp": TIMESTAMP,
        "sensor": SensorRef(sensor_id="SIM-RADAR-01", sensor_type=SensorType.SIMULATED_RADAR),
        "position": GeoPosition(latitude=53.3508, longitude=-6.2603, altitude_m=120.0),
        "measurement": Measurement(position_uncertainty_m=15.0),
        "quality": Quality(confidence=0.87),
    }
    return Detection(**{**base, **overrides})


def test_minimal_detection_is_valid_without_inferred_blocks() -> None:
    """kinematics and classification are optional. A source that measures only position must
    be able to produce a valid detection — that is the common case for real sensors."""
    detection = make_detection()

    assert detection.kinematics is None
    assert detection.classification is None
    assert detection.schema_version == DETECTION_SCHEMA_VERSION


def test_schema_version_is_carried_on_every_detection() -> None:
    """Asserted against the constant, not a literal.

    What matters is that every detection records the version it was produced under, so an
    adapter written against an older one stays identifiable. Pinning the string here would make
    a routine additive bump fail a test that is not about the number, and the pressure would
    then be on the number rather than on the change.
    """
    assert make_detection().schema_version == DETECTION_SCHEMA_VERSION
    assert re.fullmatch(r"\d+\.\d+\.\d+", DETECTION_SCHEMA_VERSION)


def test_detection_is_immutable() -> None:
    """A detection is a historical fact. Correcting one means emitting a new one."""
    detection = make_detection()

    with pytest.raises(ValidationError):
        detection.position = GeoPosition(latitude=0.0, longitude=0.0, altitude_m=0.0)


def test_naive_timestamp_is_rejected() -> None:
    """FM-012. An ambiguous timestamp produces velocity estimates wrong by the timezone
    offset, which is far cheaper to reject here than to diagnose downstream."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_detection(timestamp=datetime(2026, 8, 8, 12, 0, 0))


def test_non_utc_timestamp_is_normalised_not_rejected() -> None:
    tz = timezone(timedelta(hours=5, minutes=30))
    detection = make_detection(timestamp=datetime(2026, 8, 8, 17, 30, 1, 250_000, tzinfo=tz))

    assert detection.timestamp == TIMESTAMP
    assert detection.timestamp.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("latitude", 90.1),
        ("latitude", -90.1),
        ("longitude", 180.1),
        ("longitude", -180.1),
    ],
)
def test_out_of_range_coordinates_are_rejected(field: str, value: float) -> None:
    coords = {"latitude": 53.35, "longitude": -6.26, "altitude_m": 100.0, field: value}
    with pytest.raises(ValidationError):
        GeoPosition(**coords)


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidence_must_be_a_probability(confidence: float) -> None:
    with pytest.raises(ValidationError):
        Quality(confidence=confidence)


def test_unknown_fields_are_rejected() -> None:
    """FM-011. A source emitting a field we do not know about is a schema mismatch, and
    silently ignoring it would hide an adapter written against the wrong version."""
    with pytest.raises(ValidationError):
        make_detection(rcs_dbsm=-12.0)


def test_negative_speed_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Kinematics(speed_mps=-1.0)


@pytest.mark.parametrize("heading", [-0.1, 360.0, 400.0])
def test_heading_must_be_in_half_open_compass_range(heading: float) -> None:
    """360 is rejected as a duplicate encoding of 0 — permitting both makes equality
    comparisons and angle differences quietly inconsistent."""
    with pytest.raises(ValidationError):
        Kinematics(heading_deg=heading)


def test_radial_velocity_may_be_negative() -> None:
    """Range rate is signed: negative means closing on the sensor. Unlike speed, this is a
    quantity a Doppler-capable sensor genuinely measures."""
    kinematics = Kinematics(radial_velocity_mps=-14.0, provenance=Provenance.MEASURED)

    assert kinematics.radial_velocity_mps == -14.0


def test_covariance_must_be_three_by_three() -> None:
    with pytest.raises(ValidationError, match="3x3"):
        Measurement(position_uncertainty_m=15.0, position_covariance_enu=[[1.0, 0.0], [0.0, 1.0]])


def test_covariance_must_be_symmetric() -> None:
    """An asymmetric covariance is not a covariance. Accepting one would give the filter a
    matrix it cannot invert meaningfully."""
    with pytest.raises(ValidationError, match="symmetric"):
        Measurement(
            position_uncertainty_m=15.0,
            position_covariance_enu=[[100.0, 5.0, 0.0], [1.0, 100.0, 0.0], [0.0, 0.0, 25.0]],
        )


def test_covariance_variances_must_be_non_negative() -> None:
    with pytest.raises(ValidationError, match="non-negative"):
        Measurement(
            position_uncertainty_m=15.0,
            position_covariance_enu=[[-1.0, 0.0, 0.0], [0.0, 100.0, 0.0], [0.0, 0.0, 25.0]],
        )


def test_anisotropic_covariance_is_accepted() -> None:
    """The case the scalar cannot express: precise in range, imprecise in cross-range."""
    measurement = Measurement(
        position_uncertainty_m=40.0,
        position_covariance_enu=[[2500.0, 0.0, 0.0], [0.0, 25.0, 0.0], [0.0, 0.0, 400.0]],
    )

    assert measurement.position_covariance_enu[0][0] == 2500.0
    assert measurement.position_covariance_enu[1][1] == 25.0


def test_without_inferred_fields_strips_both_blocks() -> None:
    """The instrument the provenance guard test uses. See ADR 0002."""
    detection = make_detection(
        kinematics=Kinematics(
            speed_mps=32.0, heading_deg=185.0, provenance=Provenance.SIMULATED_TRUTH
        ),
        classification=ClassificationHint(
            label=ClassificationLabel.SMALL_UAS,
            confidence=0.9,
            provenance=Provenance.SIMULATED_TRUTH,
        ),
    )

    stripped = detection.without_inferred_fields()

    assert stripped.kinematics is None
    assert stripped.classification is None
    # Everything a real sensor genuinely measures must survive untouched.
    assert stripped.detection_id == detection.detection_id
    assert stripped.timestamp == detection.timestamp
    assert stripped.position == detection.position
    assert stripped.measurement == detection.measurement
    assert stripped.quality == detection.quality
    assert stripped.sensor == detection.sensor


def test_without_inferred_fields_does_not_mutate_the_original() -> None:
    detection = make_detection(kinematics=Kinematics(speed_mps=32.0))

    detection.without_inferred_fields()

    assert detection.kinematics is not None


def test_simulated_sensor_types_are_distinguishable_from_real_ones() -> None:
    """Provenance must be visible in the data. The tracker does not branch on it, but
    diagnostics, the UI and any future audit export need to be able to tell."""
    assert SensorType.SIMULATED_RADAR.is_simulated
    assert SensorType.SIMULATED_EO_IR.is_simulated
    assert not SensorType.RADAR.is_simulated
    assert not SensorType.ACOUSTIC.is_simulated


def test_kinematics_provenance_defaults_to_derived() -> None:
    """Derived is the honest default: speed and heading are normally computed, not measured."""
    assert Kinematics(speed_mps=10.0).provenance is Provenance.DERIVED


def test_round_trips_through_json() -> None:
    """The schema crosses a process boundary on its way to the event log and the API, so
    serialisation fidelity is part of the contract."""
    detection = make_detection(
        kinematics=Kinematics(speed_mps=32.0, heading_deg=185.0),
        classification=ClassificationHint(
            label=ClassificationLabel.UNKNOWN_UAS, confidence=0.61
        ),
    )

    assert Detection.model_validate_json(detection.model_dump_json()) == detection
