"""Risk grading calibration: the shape of the curves, not just their endpoints.

`test_risk.py` protects the engine's safety properties — absence of evidence never lowers a
score, only confident benign identification discounts, corroboration cannot create concern.
This file protects something different and easier to lose: **the curves keep the shape their
design says they have, all the way along**.

That distinction is not academic. The time-to-protected ramp shipped inverted — shallow where
it was documented to be steep — and every endpoint test passed throughout, because the
endpoints were right and only the middle was wrong. A contact four minutes out scored 39 when
the design intended 4. Endpoint assertions cannot catch that; monotonicity and mid-curve
assertions can, and that is what is here.

The figures below are deliberately expressed as *relationships* (this is below that, this
band contains this) rather than as exact scores wherever a relationship says the real thing.
A calibration test that pins an exact number to a tuning constant fails on every legitimate
retune and teaches people to update the expected value without reading it.
"""

from __future__ import annotations

import pytest

from backend.risk.engine import RiskEngine, build_risk_engine
from backend.risk.factors import (
    BENIGN_LABELS,
    FactorPolicy,
    SensorCorroborationFactor,
    TimeToProtectedFactor,
    TrackRisk,
    ZoneIncursionFactor,
    mitigation_for,
)
from backend.tracking.models import RiskLevel
from backend.zones.geometry import EnuPoint, EnuVelocity, SiteGeometry, load_site_geometry
from sensors.schemas.detection import ClassificationLabel

BIRD = ClassificationLabel.BIRD
VEHICLE = ClassificationLabel.VEHICLE
SMALL_UAS = ClassificationLabel.SMALL_UAS


_SITE = load_site_geometry("fictional_test_range")
PROTECTED_RADIUS_M = _SITE.site.protected_radius_m
"""Loaded once at module scope so the placement helper below can offset by it without needing
the fixture — time-to-boundary is measured to the protected area's edge, not to the origin."""


@pytest.fixture(scope="module")
def geometry() -> SiteGeometry:
    return _SITE


@pytest.fixture(scope="module")
def engine() -> RiskEngine:
    return build_risk_engine()


def track(**overrides) -> TrackRisk:
    base = {
        "track_id": "TRK-00001",
        "position": EnuPoint(8_000.0, 8_000.0, 300.0),
        "velocity": EnuVelocity(0.0, 5.0, 0.0),
        "speed_mps": 5.0,
        "altitude_m": 300.0,
        "detection_count": 20,
        "track_age_s": 120.0,
        "coasting": False,
        "track_confidence": 0.9,
    }
    return TrackRisk(**{**base, **overrides})


def approaching_for(seconds: float, speed_mps: float = 25.0, **overrides) -> TrackRisk:
    """A track flying straight at the site, positioned to arrive in `seconds`.

    Placed by time rather than distance because time-to-boundary is what the factor under test
    actually reads, and converting in the test keeps the intent legible at the call site.
    """
    return track(
        # Offset by the protected radius: the factor measures time to the *boundary*, so
        # placing the track `seconds * speed` from the origin would arrive early by however
        # wide the protected area is.
        position=EnuPoint(0.0, PROTECTED_RADIUS_M + seconds * speed_mps, 150.0),
        velocity=EnuVelocity(0.0, -speed_mps, 0.0),
        speed_mps=speed_mps,
        altitude_m=150.0,
        **overrides,
    )


# --------------------------------------------------------------------------------------
# Time-to-protected: the curve that shipped inverted
# --------------------------------------------------------------------------------------


def _time_factor(**parameters: float) -> TimeToProtectedFactor:
    return TimeToProtectedFactor(FactorPolicy(weight=1.0, parameters=parameters))


@pytest.mark.parametrize("seconds", [45.0, 60.0, 90.0, 120.0, 180.0, 240.0, 290.0])
def test_urgency_never_rises_as_the_contact_gets_further_out(
    geometry: SiteGeometry, seconds: float
) -> None:
    """Monotonicity, checked pairwise along the whole curve rather than at its ends."""
    factor = _time_factor()
    nearer = factor.assess(approaching_for(seconds - 15.0), geometry).activation
    further = factor.assess(approaching_for(seconds), geometry).activation

    assert nearer >= further


def test_urgency_is_shallow_far_out_and_steep_close_in(geometry: SiteGeometry) -> None:
    """The shape the factor's own docstring promises.

    Regression: the exponent was 0.5 rather than 2.0, which inverts this — the curve moved
    fastest out near the horizon, where the docstring says the difference "barely matters",
    and slowest coming in through the urgent end, where it says the difference "matters
    enormously".
    """
    factor = _time_factor(horizon_s=300.0, urgent_s=45.0)

    def activation(seconds: float) -> float:
        return factor.assess(approaching_for(seconds), geometry).activation

    far_step = activation(240.0) - activation(300.0)
    near_step = activation(60.0) - activation(120.0)

    assert near_step > far_step, (
        "urgency must climb faster approaching the urgent threshold than it does out at the "
        f"horizon; got a {near_step:.3f} rise close in against {far_step:.3f} far out"
    )


def test_a_contact_four_minutes_out_is_not_most_of_the_way_to_critical(
    geometry: SiteGeometry,
) -> None:
    """The concrete symptom the inverted curve produced.

    Four minutes of warning on a straight-line course is information, not an emergency. Under
    the inverted exponent this activated at 0.485 — which, at the configured weight and with
    engagement taking the group maximum, put an ordinary distant contact within a few points
    of MEDIUM on geometry that says only "inbound, eventually".
    """
    activation = _time_factor(horizon_s=300.0, urgent_s=45.0).assess(
        approaching_for(240.0), geometry
    ).activation

    assert activation < 0.2


def test_urgency_saturates_at_and_inside_the_urgent_threshold(geometry: SiteGeometry) -> None:
    factor = _time_factor(horizon_s=300.0, urgent_s=45.0)

    assert factor.assess(approaching_for(45.0), geometry).activation == pytest.approx(1.0)
    assert factor.assess(approaching_for(20.0), geometry).activation == pytest.approx(1.0)


def test_sharpness_is_configurable_and_one_means_linear(geometry: SiteGeometry) -> None:
    """A site may retune the curve, and the linear case is the readable reference point."""
    linear = _time_factor(horizon_s=300.0, urgent_s=45.0, sharpness=1.0)
    midpoint = approaching_for(45.0 + (300.0 - 45.0) / 2.0)

    assert linear.assess(midpoint, geometry).activation == pytest.approx(0.5, abs=0.02)


def test_beyond_the_horizon_is_silent_not_merely_small(geometry: SiteGeometry) -> None:
    contribution = _time_factor(horizon_s=300.0).assess(approaching_for(400.0), geometry)

    assert contribution.activation == 0.0
    assert "beyond the assessment horizon" in contribution.detail


# --------------------------------------------------------------------------------------
# End-to-end grading: canonical situations land in the intended bands
# --------------------------------------------------------------------------------------


def band(engine: RiskEngine, geometry: SiteGeometry, risk: TrackRisk) -> RiskLevel:
    """The level a score implies, before hysteresis.

    Hysteresis is a function of history and is tested in `test_risk_hysteresis.py`; what this
    file cares about is where a *fresh* assessment of a given situation lands.
    """
    score = engine.assess(risk, geometry).score
    for level, floor in (
        (RiskLevel.CRITICAL, 87),
        (RiskLevel.HIGH, 70),
        (RiskLevel.MEDIUM, 45),
        (RiskLevel.LOW, 20),
    ):
        if score >= floor:
            return level
    return RiskLevel.NONE


def test_a_distant_transiting_contact_stays_at_the_floor(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """The commonest thing in the sky must not occupy an operator."""
    crossing = track(
        position=EnuPoint(6_000.0, 6_000.0, 400.0),
        velocity=EnuVelocity(30.0, 0.0, 0.0),
        speed_mps=30.0,
        altitude_m=400.0,
    )

    assert band(engine, geometry, crossing) in (RiskLevel.NONE, RiskLevel.LOW)


def test_a_contact_inside_the_protected_area_is_high_or_critical(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """The thing the system exists to notice."""
    overhead = track(
        position=EnuPoint(0.0, 50.0, 40.0),
        velocity=EnuVelocity(0.0, -5.0, 0.0),
        speed_mps=5.0,
        altitude_m=40.0,
    )

    assert band(engine, geometry, overhead) in (RiskLevel.HIGH, RiskLevel.CRITICAL)


def test_grading_is_ordered_across_a_closing_approach(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """Score must not fall as a contact closes — the property an operator assumes without
    being told, and the one a badly shaped curve breaks silently."""
    scores = [
        engine.assess(approaching_for(seconds), geometry).score
        for seconds in (280.0, 200.0, 140.0, 90.0, 50.0, 20.0)
    ]

    assert scores == sorted(scores), f"score fell while the contact closed: {scores}"


def test_an_identified_uas_outranks_an_unknown_on_identical_geometry(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    geometry_only = approaching_for(90.0)
    identified = approaching_for(
        90.0,
        classification={SMALL_UAS: 1.0},
        classification_confidence=0.9,
        classification_assessed=True,
    )

    assert engine.assess(identified, geometry).score > engine.assess(geometry_only, geometry).score


# --------------------------------------------------------------------------------------
# Multi-sensor corroboration
# --------------------------------------------------------------------------------------


def _corroboration(**parameters: float) -> SensorCorroborationFactor:
    return SensorCorroborationFactor(FactorPolicy(weight=1.0, parameters=parameters))


def test_a_single_sensor_track_earns_no_corroboration(geometry: SiteGeometry) -> None:
    contribution = _corroboration().assess(track(contributing_sensor_count=1), geometry)

    assert contribution.activation == 0.0
    assert "single sensor" in contribution.detail


def test_corroboration_rises_with_independent_sensors(geometry: SiteGeometry) -> None:
    factor = _corroboration(corroborating_sensors=3.0)
    activations = [
        factor.assess(track(contributing_sensor_count=n), geometry).activation
        for n in (1, 2, 3, 4)
    ]

    assert activations == sorted(activations)
    assert activations[0] == 0.0
    assert activations[2] == pytest.approx(1.0)
    assert activations[3] == pytest.approx(1.0), "must saturate, not exceed 1"


def test_corroboration_cannot_create_concern_on_its_own(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """The CORROBORATING contract: it amplifies an existing concern and cannot invent one.

    A contact held by every sensor on site, parked far away going nowhere, is a well-observed
    non-event.
    """
    far_and_harmless = track(
        position=EnuPoint(9_000.0, 9_000.0, 500.0),
        velocity=EnuVelocity(0.0, 0.0, 0.0),
        speed_mps=0.0,
        altitude_m=500.0,
        contributing_sensor_count=4,
    )

    assert engine.assess(far_and_harmless, geometry).score == 0


def test_corroboration_raises_an_existing_concern(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    one_sensor = approaching_for(80.0, contributing_sensor_count=1)
    three_sensors = approaching_for(80.0, contributing_sensor_count=3)

    assert engine.assess(three_sensors, geometry).score > engine.assess(one_sensor, geometry).score


def test_many_detections_from_one_sensor_are_not_corroboration(
    geometry: SiteGeometry,
) -> None:
    """Persistence and corroboration are different claims, and a chatty sensor must not be
    able to impersonate agreement."""
    chatty = track(detection_count=500, contributing_sensor_count=1)

    assert _corroboration().assess(chatty, geometry).activation == 0.0


# --------------------------------------------------------------------------------------
# Zone severity is configuration, and mitigation is deterministic
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("north_m", "parameter"),
    [(400.0, "protected_activation"), (1_500.0, "restricted_activation")],
)
def test_zone_severity_activations_are_configurable(
    geometry: SiteGeometry, north_m: float, parameter: str
) -> None:
    """A site decides what presence in each kind of zone means.

    These were hardcoded, which put a deployment judgement in Python: a range whose restricted
    ring is routinely transited by its own traffic needs a different figure from one where any
    incursion is exceptional, and neither should have to edit code to say so.
    """
    inside = track(position=EnuPoint(0.0, north_m, 80.0), altitude_m=80.0)
    occupied = geometry.most_consequential_zone(inside.position)
    assert occupied is not None, "test fixture site no longer has a zone at this point"

    strict = ZoneIncursionFactor(FactorPolicy(weight=1.0, parameters={parameter: 0.95}))
    relaxed = ZoneIncursionFactor(FactorPolicy(weight=1.0, parameters={parameter: 0.10}))

    assert strict.assess(inside, geometry).activation == pytest.approx(0.95)
    assert relaxed.assess(inside, geometry).activation == pytest.approx(0.10)


def test_monitored_airspace_still_escalates_nothing_by_default(
    geometry: SiteGeometry,
) -> None:
    """The default that keeps ordinary traffic off the operator's list: being in watched
    airspace is the ground state at a site that bothered to define watched airspace."""
    inside_monitored = track(position=EnuPoint(0.0, 3_000.0, 80.0), altitude_m=80.0)
    contribution = ZoneIncursionFactor(FactorPolicy(weight=1.0)).assess(
        inside_monitored, geometry
    )

    assert contribution.activation == 0.0
    assert "monitored airspace only" in contribution.detail


def test_the_named_benign_label_does_not_depend_on_set_ordering() -> None:
    """Deterministic tie-breaking in the discount's explanation.

    Regression: the benign labels were a `frozenset`, so an exact tie between them resolved by
    enum hash order — which varies with `PYTHONHASHSEED`. The same replayed log could then
    explain the same discount with a different label from one run to the next (ADR 0008).
    """
    tied = track(
        classification={BIRD: 0.5, VEHICLE: 0.5},
        classification_confidence=0.8,
        classification_assessed=True,
    )

    _, reason = mitigation_for(tied, 0.35)

    assert reason is not None
    assert str(BENIGN_LABELS[0]).replace("_", " ") in reason


def test_benign_labels_are_ordered_not_a_set() -> None:
    """The property the determinism above rests on, asserted directly so a future edit back to
    a set fails here rather than intermittently somewhere else."""
    assert isinstance(BENIGN_LABELS, tuple)
