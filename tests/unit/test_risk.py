"""Risk: factor behaviour, combination, mitigation asymmetry and calibration.

The safety property this file exists to protect is stated once, here, and then tested from
several directions: **absence of evidence never reduces risk**. Only a positive, confident
identification of something harmless may lower a score, and never to zero. A system that
scored an unknown contact low because it did not know what it was would be quietest exactly
when it should be loudest.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import backend.risk.factors
import backend.risk.service
from backend.common.config import ConfigError, ConfigPaths
from backend.risk.engine import RiskEngine, RiskPolicy, build_risk_engine, load_risk_policy
from backend.risk.factors import (
    ClosureFactor,
    EscalationFactor,
    FactorPolicy,
    LoiterFactor,
    LowAltitudePenetrationFactor,
    TrackRisk,
)
from backend.zones.geometry import EnuPoint, EnuVelocity, SiteGeometry, load_site_geometry
from backend.zones.site import GeodeticReference, Site
from sensors.schemas.detection import ClassificationLabel

SMALL_UAS = ClassificationLabel.SMALL_UAS
BIRD = ClassificationLabel.BIRD


@pytest.fixture(scope="module")
def geometry() -> SiteGeometry:
    return load_site_geometry("fictional_test_range")


@pytest.fixture(scope="module")
def engine() -> RiskEngine:
    return build_risk_engine()


@pytest.fixture(scope="module")
def policy() -> RiskPolicy:
    return load_risk_policy()


def track(**overrides) -> TrackRisk:
    """A well-supported track well outside everything, heading nowhere in particular.

    The deliberately boring baseline: every test below states its own scenario by overriding
    exactly the fields that scenario is about, so what each one is testing is visible in the
    call rather than buried in a fixture.
    """
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
        "classification": {},
        "classification_confidence": 0.0,
        "classification_assessed": False,
        "path_length_m": 0.0,
        "net_displacement_m": 0.0,
        "loiter_window_s": 0.0,
    }
    return TrackRisk(**{**base, **overrides})


def inbound(distance_m: float, speed_mps: float = 25.0, **overrides) -> TrackRisk:
    """A track that distance out, flying straight at the site."""
    return track(
        position=EnuPoint(0.0, distance_m, 150.0),
        velocity=EnuVelocity(0.0, -speed_mps, 0.0),
        speed_mps=speed_mps,
        altitude_m=150.0,
        **overrides,
    )


def score(engine: RiskEngine, geometry: SiteGeometry, risk: TrackRisk) -> int:
    return engine.assess(risk, geometry).score


# --------------------------------------------------------------------------------------
# The safety property: absence of evidence never reduces risk
# --------------------------------------------------------------------------------------


def test_an_unknown_contact_is_not_discounted(engine: RiskEngine, geometry: SiteGeometry) -> None:
    """The whole module's safety posture in one assertion.

    An unclassified contact on a bad trajectory scores exactly what its geometry earns. A
    model that discounted it for being unidentified would be silent on precisely the case
    nobody can rule out.
    """
    unknown = inbound(600.0)
    assessment = engine.assess(unknown, geometry)

    assert assessment.mitigation == 1.0
    assert assessment.score > 0


def test_only_a_confident_benign_identification_discounts(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    unknown = engine.assess(inbound(600.0), geometry)
    bird = engine.assess(
        inbound(
            600.0,
            classification={BIRD: 0.95, SMALL_UAS: 0.05},
            classification_confidence=0.9,
            classification_assessed=True,
        ),
        geometry,
    )

    assert bird.score < unknown.score
    assert bird.mitigation < 1.0


def test_a_low_confidence_benign_guess_barely_discounts(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """The discount scales with belief, so a hesitant guess cannot wave a contact through."""
    confident = engine.assess(
        inbound(
            600.0,
            classification={BIRD: 0.95},
            classification_confidence=0.9,
            classification_assessed=True,
        ),
        geometry,
    )
    hesitant = engine.assess(
        inbound(
            600.0,
            classification={BIRD: 0.95},
            classification_confidence=0.15,
            classification_assessed=True,
        ),
        geometry,
    )

    assert hesitant.mitigation > confident.mitigation
    assert hesitant.score > confident.score


def test_a_bird_in_the_protected_zone_is_still_reported(
    engine: RiskEngine, geometry: SiteGeometry, policy: RiskPolicy
) -> None:
    """The discount is bounded and never reaches zero.

    Classification can be wrong, and a bird penetrating a protected zone is a fact worth an
    operator's attention. Discounting to nothing would delete the track from the picture on
    the strength of a guess.
    """
    assessment = engine.assess(
        inbound(
            50.0,
            classification={BIRD: 1.0},
            classification_confidence=1.0,
            classification_assessed=True,
        ),
        geometry,
    )

    assert assessment.mitigation >= policy.min_mitigation
    assert assessment.score > 0


def test_no_factor_can_lower_a_score_on_its_own(engine: RiskEngine, geometry: SiteGeometry) -> None:
    """Every factor is escalating. Adding evidence must never reduce the total.

    Checked by walking a contact inwards: each step adds activation to one factor or another
    and never removes any, so the sequence must be non-decreasing.
    """
    scores = [score(engine, geometry, inbound(d)) for d in (5_000, 3_000, 1_500, 800, 300, 50)]

    assert scores == sorted(scores), f"score fell as the contact closed: {scores}"


# --------------------------------------------------------------------------------------
# Combination: noisy-OR rather than a sum or a maximum
# --------------------------------------------------------------------------------------


def test_corroboration_raises_the_score_above_any_single_factor(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """A maximum rule would report these identically. They are not identical situations."""
    geometry_only = engine.assess(inbound(700.0), geometry)
    geometry_and_identity = engine.assess(
        inbound(
            700.0,
            classification={SMALL_UAS: 0.9},
            classification_confidence=0.85,
            classification_assessed=True,
        ),
        geometry,
    )

    assert geometry_and_identity.score > geometry_only.score


def test_many_mild_factors_do_not_reach_a_severe_score(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """The failure mode a weighted sum has and noisy-OR does not.

    A contact that is mildly notable in several respects at once must not score like one that
    is inside the protected zone. "Slightly unusual in five ways" is not a critical threat.
    """
    mild = track(
        position=EnuPoint(0.0, 3_200.0, 200.0),
        velocity=EnuVelocity(0.0, -3.0, 0.0),
        speed_mps=3.0,
        altitude_m=200.0,
        classification={SMALL_UAS: 0.4, ClassificationLabel.UNKNOWN: 0.6},
        classification_confidence=0.3,
        classification_assessed=True,
    )
    severe = inbound(30.0, speed_mps=30.0)

    assert score(engine, geometry, mild) < score(engine, geometry, severe)


def test_the_score_saturates_rather_than_overflowing(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """Every factor at once still lands inside the scale."""
    worst = track(
        position=EnuPoint(0.0, 20.0, 15.0),
        velocity=EnuVelocity(0.0, -30.0, 0.0),
        speed_mps=30.0,
        altitude_m=15.0,
        classification={SMALL_UAS: 1.0},
        classification_confidence=1.0,
        classification_assessed=True,
        path_length_m=900.0,
        net_displacement_m=20.0,
        loiter_window_s=180.0,
    )
    assessment = engine.assess(worst, geometry)

    assert 0 <= assessment.score <= 100
    assert assessment.score > 80


# --------------------------------------------------------------------------------------
# Individual factors: each isolates something the others cannot see
# --------------------------------------------------------------------------------------


def test_closure_distinguishes_inbound_from_transiting(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """Same range, same speed, different situation."""
    approaching = inbound(1_200.0, speed_mps=20.0)
    crossing = track(
        position=EnuPoint(0.0, 1_200.0, 150.0),
        velocity=EnuVelocity(20.0, 0.0, 0.0),
        speed_mps=20.0,
        altitude_m=150.0,
    )

    assert score(engine, geometry, approaching) > score(engine, geometry, crossing)


def test_a_stationary_contact_close_in_still_scores(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """Proximity is separate from closure precisely so this case is not scored at zero.

    An object holding station just outside the fence is not closing at all, and is exactly
    what an operator most needs raised.
    """
    holding = track(
        position=EnuPoint(0.0, 400.0, 100.0),
        velocity=EnuVelocity(0.0, 0.0, 0.0),
        speed_mps=0.0,
        altitude_m=100.0,
    )

    assert score(engine, geometry, holding) > 0


def test_loitering_is_distinguished_from_transiting_the_same_distance(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """Same ground covered, same position — one went somewhere and one did not."""
    common = {
        "position": EnuPoint(0.0, 900.0, 120.0),
        "velocity": EnuVelocity(6.0, 0.0, 0.0),
        "speed_mps": 6.0,
        "altitude_m": 120.0,
        "loiter_window_s": 120.0,
        "path_length_m": 700.0,
    }
    loitering = track(**common, net_displacement_m=40.0)
    transiting = track(**common, net_displacement_m=690.0)

    assert score(engine, geometry, loitering) > score(engine, geometry, transiting)


def test_low_altitude_only_counts_when_close(engine: RiskEngine, geometry: SiteGeometry) -> None:
    """Gated deliberately: low and far is most of general aviation and every bird."""
    contributions = {
        distance: next(
            factor
            for factor in engine.assess(
                track(
                    position=EnuPoint(0.0, distance, 25.0),
                    velocity=EnuVelocity(0.0, 0.0, 0.0),
                    speed_mps=0.0,
                    altitude_m=25.0,
                ),
                geometry,
            ).factors
            if factor.code == "low_altitude_near_asset"
        )
        for distance in (400.0, 9_000.0)
    }

    assert contributions[400.0].activation > 0.0
    assert contributions[9_000.0].activation == 0.0


def test_a_new_track_cannot_reach_a_high_score_on_persistence(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """Persistence is corroboration, not concern. One cycle of alarming geometry is still
    alarming, but it should not also be *corroborated*."""
    fresh = inbound(700.0, detection_count=1, track_age_s=0.5)
    established = inbound(700.0, detection_count=40, track_age_s=200.0)

    assert score(engine, geometry, fresh) < score(engine, geometry, established)


# --------------------------------------------------------------------------------------
# Explainability
# --------------------------------------------------------------------------------------


def test_every_score_arrives_with_reasons(engine: RiskEngine, geometry: SiteGeometry) -> None:
    assessment = engine.assess(inbound(500.0), geometry)

    assert assessment.score > 0
    assert assessment.reasons
    assert len(assessment.codes) == len(assessment.reasons)


def test_reasons_are_ordered_by_what_actually_moved_the_score(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """Ordered by marginal contribution, not configured weight.

    Under noisy-OR a factor arriving alongside strong others adds less than the same factor
    arriving alone, and an operator reading top-down should get them in the order they
    mattered.
    """
    assessment = engine.assess(inbound(400.0), geometry)
    material = [f for f in assessment.factors if f.material]
    contributions = sorted((f.contribution for f in material), reverse=True)

    assert [f.contribution for f in sorted(material, key=lambda f: -f.contribution)] == (
        contributions
    )
    assert assessment.reasons[0] == max(material, key=lambda f: f.contribution).detail


def test_silent_factors_are_retained_so_a_check_is_visible(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """A concern that was checked and found absent is different from one never considered.

    The baseline track is 11 km out doing nothing, so every *engagement* and *behaviour* factor
    should be silent — and each should still be present, with the sentence saying what was
    looked for.
    """
    assessment = engine.assess(track(), geometry)
    by_code = {factor.code: factor for factor in assessment.factors}

    assert "loitering_near_asset" in by_code
    assert not by_code["loitering_near_asset"].material
    assert by_code["loitering_near_asset"].detail
    assert not by_code["proximity_to_protected_area"].material


def test_a_discount_is_explained_like_everything_else(
    engine: RiskEngine, geometry: SiteGeometry
) -> None:
    """An operator who sees a score drop is owed the same explanation as one who sees it rise."""
    assessment = engine.assess(
        inbound(
            600.0,
            classification={BIRD: 0.95},
            classification_confidence=0.9,
            classification_assessed=True,
        ),
        geometry,
    )

    assert "benign_classification" in assessment.codes


# --------------------------------------------------------------------------------------
# Confidence is a separate number
# --------------------------------------------------------------------------------------


def test_confidence_does_not_change_the_score(engine: RiskEngine, geometry: SiteGeometry) -> None:
    """The property ADR 0013 turns on.

    A thinly-supported track on alarming geometry must produce the same score as a
    well-supported one in the same place — and say, separately, that it is less certain.
    Folding confidence into the score would suppress exactly the case that needs an operator.
    """
    # Varying only what feeds confidence and nothing else. Detection count is deliberately
    # held fixed: it feeds the persistence *factor* too, so changing it would move the score
    # legitimately and the test would prove nothing about the property in question.
    thin = engine.assess(inbound(500.0, coasting=True, track_confidence=0.2), geometry)
    solid = engine.assess(inbound(500.0, coasting=False, track_confidence=0.95), geometry)

    assert thin.score == solid.score
    assert thin.confidence < solid.confidence


def test_coasting_reduces_confidence(engine: RiskEngine, geometry: SiteGeometry) -> None:
    observed = engine.assess(inbound(500.0), geometry)
    coasting = engine.assess(inbound(500.0, coasting=True), geometry)

    assert coasting.confidence < observed.confidence


def test_an_assessed_track_says_so_even_at_zero(engine: RiskEngine, geometry: SiteGeometry) -> None:
    """ "We looked and nothing here is concerning" is a finding. "Not assessed" is not.

    Doubles as the corroboration regression: this track *is* well established — twenty
    detections over two minutes — and the persistence factor duly reports so. It must still
    score zero, because nothing about its geometry or behaviour is concerning and corroboration
    cannot create concern that was never there. The first version of the engine scored this 25.
    """
    assessment = engine.assess(track(), geometry)
    persistence = next(f for f in assessment.factors if f.code == "persistent_track")

    assert assessment.assessed is True
    assert persistence.material, "expected the persistence factor to have something to say"
    assert assessment.score == 0


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def test_the_shipped_policy_loads_and_covers_every_factor(policy: RiskPolicy) -> None:
    engine = build_risk_engine()

    assert len(engine.factors) == len(policy.factors)
    assert {factor.code for factor in engine.factors} == set(policy.factors)


def test_every_band_has_a_hysteresis_gap(policy: RiskPolicy) -> None:
    """A band whose fall threshold equals its rise threshold has no hysteresis at all, which
    is the flapping ADR 0007 exists to prevent."""
    for band in policy.hysteresis.bands:
        assert band.fall < band.rise, f"{band.level} has no hysteresis gap"


def test_deescalation_is_slower_than_escalation(policy: RiskPolicy) -> None:
    """Asymmetric on purpose: a genuine threat must not be held back by a timer, and a
    de-escalation must not happen on one quiet cycle."""
    assert policy.hysteresis.deescalate_dwell_s > policy.hysteresis.escalate_dwell_s


# --------------------------------------------------------------------------------------
# Reason codes must earn their place in the list
# --------------------------------------------------------------------------------------


def _risk_with_benign_belief(benign_mass: float, confidence: float) -> TrackRisk:
    """An inbound track the classifier thinks is *slightly* benign."""
    return inbound(
        1_200.0,
        classification={"bird": benign_mass, "small_uas": 1.0 - benign_mass},
        classification_confidence=confidence,
        classification_assessed=True,
    )


def test_an_immaterial_discount_is_applied_but_not_reported() -> None:
    """A reason line asserting a discount nobody can see is worse than no line.

    `_MATERIAL_MITIGATION` was defined for exactly this and never wired in, so a 0.1% discount
    produced "Discounted: classified bird (0% of the distribution at 50% confidence)" — a
    sentence claiming a judgement the system did not really make, quoting a percentage that had
    rounded away to nothing.
    """
    engine = build_risk_engine()
    geometry = load_site_geometry("fictional_test_range")

    assessment = engine.assess(_risk_with_benign_belief(0.004, 0.5), geometry)

    assert assessment.mitigation < 1.0, "the discount is still applied to the score"
    assert not any(f.code == "benign_classification" for f in assessment.factors), (
        "an invisible discount must not appear in the operator's reason list"
    )


def test_a_material_discount_is_still_reported() -> None:
    """The converse, so the fix cannot be over-applied into silence: an operator who sees a
    score drop is owed the explanation."""
    engine = build_risk_engine()
    geometry = load_site_geometry("fictional_test_range")

    assessment = engine.assess(_risk_with_benign_belief(0.9, 0.9), geometry)

    reasons = [f for f in assessment.factors if f.code == "benign_classification"]
    assert reasons, "a substantial benign classification must be explained"
    assert "bird" in reasons[0].detail


# --------------------------------------------------------------------------------------
# Policy parameters are configuration, and configuration is validated
# --------------------------------------------------------------------------------------


def test_a_negative_parameter_is_rejected_at_load() -> None:
    """The quiet failure: `fast_mps: -25` does not disable the closing-velocity factor, it
    inverts it. A target closing at 20 m/s scored zero, and nothing anywhere said so.

    `parameters` was the one configuration surface in the system with no validation at all.
    """
    with pytest.raises(ValueError, match="must not be negative"):
        FactorPolicy(weight=0.5, parameters={"fast_mps": -25.0})


def test_a_non_finite_parameter_is_rejected_at_load() -> None:
    with pytest.raises(ValueError, match="finite"):
        FactorPolicy(weight=0.5, parameters={"fast_mps": float("inf")})


def test_zero_stays_legal_because_some_floors_honestly_mean_none() -> None:
    """The validator must not overreach: several parameters are minimums where zero means
    'no minimum', and rejecting those would break legitimate configurations."""
    policy = FactorPolicy(weight=0.5, parameters={"minimum_mps": 0.0})

    assert policy.parameter("minimum_mps", 1.0) == 0.0


@pytest.mark.parametrize(
    ("factor_type", "parameters", "risk_overrides"),
    [
        (ClosureFactor, {"fast_mps": 0.0}, {}),
        (LoiterFactor, {"minimum_path_m": 0.0, "minimum_window_s": 0.0}, {"loiter_window_s": 60.0}),
    ],
)
def test_a_zero_divisor_cannot_kill_the_cycle(
    factor_type: type, parameters: dict[str, float], risk_overrides: dict
) -> None:
    """Zero is legal at load, so the division sites must survive it.

    Three of the five divisors in this module already carried a `max(1e-6, ...)` guard and two
    did not — the idiom was there, applied inconsistently. Both unguarded ones raised
    `ZeroDivisionError` from inside the assessment, which kills the risk stage and with it the
    whole pipeline cycle: the operator's picture simply stops advancing.
    """
    geometry = load_site_geometry("fictional_test_range")
    factor = factor_type(FactorPolicy(weight=0.5, parameters=parameters))

    contribution = factor.assess(inbound(400.0, **risk_overrides), geometry)

    assert 0.0 <= contribution.activation <= 1.0


def test_a_misspelled_parameter_is_rejected_rather_than_ignored(tmp_path: Path) -> None:
    """Tuning a parameter that does not exist must fail, not quietly do nothing.

    `parameters` is a free-form dict, so `extra="forbid"` cannot police it and every
    unrecognised key fell through to the factor's default. Someone setting `fast_mp` instead of
    `fast_mps` left the closing-velocity factor on 25 m/s while the file said 100 — a four-fold
    difference in a threat factor, with the policy reading as configured and the system
    behaving as though it were not.

    Factor *codes* were validated here from the beginning. This is the other half of the same
    surface.
    """
    document = json.loads((ConfigPaths.default().config_root / "risk" / "policy.json").read_text())
    document["factors"]["closing_velocity"]["parameters"]["fast_mp"] = 100.0

    (tmp_path / "risk").mkdir()
    (tmp_path / "risk" / "policy.json").write_text(json.dumps(document))
    paths = ConfigPaths(repo_root=tmp_path, config_root=tmp_path)

    with pytest.raises(ConfigError, match="has no parameter"):
        load_risk_policy(paths=paths)


def test_declared_parameters_match_what_each_factor_actually_reads() -> None:
    """The declarations must not drift from the code they describe.

    Derived from the source rather than maintained by hand on both sides: a parameter added to
    a factor without being declared would otherwise become unconfigurable — rejected by the
    loader the first time anyone tried to set it — and a declaration left behind after a
    parameter was removed would advertise tuning that does nothing.
    """
    source = ast.parse((Path(backend.risk.factors.__file__)).read_text())

    for node in source.body:
        if not isinstance(node, ast.ClassDef):
            continue
        factor = getattr(backend.risk.factors, node.name, None)
        if not (isinstance(factor, type) and issubclass(factor, EscalationFactor)):
            continue
        if factor is EscalationFactor:
            continue

        read = {
            call.args[0].value
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "parameter"
            and call.args
            and isinstance(call.args[0], ast.Constant)
        }

        assert read == set(factor.SUPPORTED_PARAMETERS), (
            f"{node.name}: SUPPORTED_PARAMETERS says {sorted(factor.SUPPORTED_PARAMETERS)} "
            f"but the code reads {sorted(read)}"
        )


# --------------------------------------------------------------------------------------
# One vertical datum, on every site
# --------------------------------------------------------------------------------------


def _site_at_elevation(elevation_m: float) -> SiteGeometry:
    return SiteGeometry(
        Site(
            site_id="elevated",
            name="Elevated site",
            reference=GeodeticReference(
                latitude=51.8971, longitude=-8.4697, altitude_m=elevation_m
            ),
            protected_radius_m=800.0,
        )
    )


@pytest.mark.parametrize("site_elevation_m", [0.0, 5.0, 200.0, 500.0, 1_500.0])
def test_low_altitude_is_judged_against_the_site_not_the_ellipsoid(
    site_elevation_m: float,
) -> None:
    """The same approach must score the same at sea level and on a plateau.

    `low_altitude_near_asset` compares against thresholds of 120 m and 30 m, which mean height
    above the site. It used to be handed the track's *ellipsoidal* altitude, so the site's own
    elevation was added to every track: at a site 500 m up, a drone 40 m over the fence read as
    "540 m; not a low-altitude approach" and the factor could never fire at all.

    The shipped site sits at 5 m, which is exactly why this looked correct.
    """
    factor = LowAltitudePenetrationFactor(
        FactorPolicy(
            weight=0.4,
            parameters={"low_m": 120.0, "very_low_m": 30.0, "gate_range_m": 1_500.0},
        )
    )
    # Forty metres above the site, three hundred metres out — a textbook low approach.
    risk = track(
        position=EnuPoint(0.0, 300.0, 40.0),
        velocity=EnuVelocity(0.0, -15.0, 0.0),
        speed_mps=15.0,
        altitude_m=40.0,
    )

    contribution = factor.assess(risk, _site_at_elevation(site_elevation_m))

    assert contribution.activation > 0.5, (
        f"a low approach stopped being one at a site {site_elevation_m} m above the ellipsoid"
    )


def test_the_risk_view_of_altitude_is_site_relative() -> None:
    """The seam itself: whatever the service hands the engine must be on the site's datum.

    Asserted on the mapping rather than only on the factor, because the factor is correct in
    isolation either way — it was the value flowing into it that carried the wrong datum.
    """
    source = Path(backend.risk.service.__file__).read_text()

    assert "altitude_m=track.enu.up_m" in source, (
        "risk is reading a vertical quantity that is not the site-relative one; "
        "track.position.altitude_m is ellipsoidal and adds the site's elevation to every track"
    )
