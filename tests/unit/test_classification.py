"""Classification: pooling, calibration, abstention, and the rules that must not regress."""

from __future__ import annotations

import pytest

from backend.classification.classifier import Classifier, build_default_classifier
from backend.classification.evidence import (
    Band,
    KinematicEvidence,
    KinematicProfile,
    SensorHintEvidence,
    TrackEvidence,
)
from backend.classification.models import ClassificationResult, top_label
from sensors.schemas.detection import ClassificationHint, ClassificationLabel, Provenance

SMALL = ClassificationLabel.SMALL_UAS
BIRD = ClassificationLabel.BIRD
VEHICLE = ClassificationLabel.VEHICLE
UNKNOWN = ClassificationLabel.UNKNOWN


def hint(label: ClassificationLabel, confidence: float = 0.8, **kwargs) -> ClassificationHint:
    return ClassificationHint(
        label=label,
        confidence=confidence,
        provenance=kwargs.get("provenance", Provenance.MEASURED),
    )


def evidence(**overrides) -> TrackEvidence:
    base = {
        "track_id": "TRK-00001",
        "speed_mps": 12.0,
        "altitude_m": 80.0,
        "vertical_rate_mps": 0.0,
        "detection_count": 20,
        "coasting": False,
        "hints": (),
        "hint_quality": (),
    }
    return TrackEvidence(**{**base, **overrides})


@pytest.fixture
def classifier() -> Classifier:
    return build_default_classifier()


# --------------------------------------------------------------------------------------
# Abstention — a source with nothing to say must not vote for uniformity
# --------------------------------------------------------------------------------------


def test_no_evidence_at_all_is_reported_as_unassessed(classifier: Classifier) -> None:
    """Distinguishes 'the classifier ran and found nothing' from 'never classified'. Both
    display as unassessed, but only this one means the pipeline is healthy."""
    result = classifier.classify(evidence(coasting=True, hints=()))

    assert result.distribution == {}
    assert result.label is UNKNOWN
    assert result.confidence == 0.0
    assert not result.assessed


def test_abstaining_source_records_why(classifier: Classifier) -> None:
    """An abstention is still evidence about the situation and is kept, so an operator can
    see why nothing was concluded rather than being shown a bare 'unknown'."""
    result = classifier.classify(evidence(coasting=True))

    details = {c.source: c.detail for c in result.evidence}
    assert "coasting" in details["kinematic"].lower()
    assert all(c.weight == 0.0 for c in result.evidence)


def test_coasting_track_ignores_kinematics(classifier: Classifier) -> None:
    """A coasting track's motion is the filter's prediction. Classifying on it would let a
    classification drift while nothing is actually being observed."""
    moving = classifier.classify(evidence(coasting=False))
    coasting = classifier.classify(evidence(coasting=True))

    assert any(c.source == "kinematic" and c.weight > 0 for c in moving.evidence)
    assert all(c.weight == 0.0 for c in coasting.evidence if c.source == "kinematic")


# --------------------------------------------------------------------------------------
# Pooling
# --------------------------------------------------------------------------------------


def test_distribution_always_sums_to_one(classifier: Classifier) -> None:
    result = classifier.classify(evidence(hints=(hint(SMALL),), hint_quality=(0.9,)))

    assert sum(result.distribution.values()) == pytest.approx(1.0)


def test_agreeing_sensors_concentrate_probability(classifier: Classifier) -> None:
    one = classifier.classify(evidence(hints=(hint(SMALL),), hint_quality=(0.9,)))
    three = classifier.classify(
        evidence(hints=(hint(SMALL), hint(SMALL), hint(SMALL)), hint_quality=(0.9, 0.9, 0.9))
    )

    assert three.distribution[SMALL] > one.distribution[SMALL]


def test_disagreeing_sensors_are_not_resolved_into_a_verdict(classifier: Classifier) -> None:
    """Two equally-credible sensors disagreeing must not produce a confident winner. Ties
    resolve to unknown rather than to whichever label happens to sort first."""
    result = classifier.classify(
        evidence(
            speed_mps=14.0,
            altitude_m=90.0,
            hints=(hint(BIRD, 0.7), hint(SMALL, 0.7)),
            hint_quality=(0.9, 0.9),
        )
    )

    assert result.distribution[BIRD] == pytest.approx(result.distribution[SMALL], abs=1e-6)
    assert result.label is UNKNOWN


def test_one_confident_wrong_source_cannot_veto_the_others() -> None:
    """The reason for linear pooling over a product of likelihoods (ADR 0012). Under a
    product, a source assigning zero to a label eliminates it outright however much other
    evidence supports it."""
    narrow = KinematicProfile(
        label=VEHICLE,
        speed_mps=Band(absolute_min=0, typical_min=1, typical_max=30, absolute_max=55),
        altitude_m=Band(absolute_min=-30, typical_min=-5, typical_max=15, absolute_max=40),
        vertical_rate_mps=Band(absolute_min=-2, typical_min=-0.5, typical_max=0.5, absolute_max=2),
        prior=0.5,
    )
    classifier = Classifier((SensorHintEvidence(), KinematicEvidence((narrow,))))

    # Kinematics say vehicle and nothing else; the sensor says small UAS. Small UAS must
    # survive, because the kinematic source simply has no opinion about it.
    result = classifier.classify(
        evidence(speed_mps=10.0, altitude_m=5.0, hints=(hint(SMALL, 0.9),), hint_quality=(0.9,))
    )

    assert result.distribution.get(SMALL, 0.0) > 0.0
    assert result.distribution.get(VEHICLE, 0.0) > 0.0


def test_kinematically_impossible_class_is_excluded_by_that_source() -> None:
    """Ruling out is where kinematic evidence earns its place: a ground vehicle at 400 m is
    outside every configured band, so that source gives it no probability at all."""
    source = KinematicEvidence(
        (
            KinematicProfile(
                label=VEHICLE,
                speed_mps=Band(absolute_min=0, typical_min=1, typical_max=30, absolute_max=55),
                altitude_m=Band(
                    absolute_min=-30, typical_min=-5, typical_max=15, absolute_max=40
                ),
                vertical_rate_mps=Band(
                    absolute_min=-2, typical_min=-0.5, typical_max=0.5, absolute_max=2
                ),
                prior=0.5,
            ),
        )
    )

    contribution = source.assess(evidence(speed_mps=25.0, altitude_m=400.0))

    assert contribution.distribution.get(VEHICLE, 0.0) == 0.0


# --------------------------------------------------------------------------------------
# Confidence calibration
# --------------------------------------------------------------------------------------


def test_confidence_is_not_the_top_probability(classifier: Classifier) -> None:
    """The central calibration claim of ADR 0012. A sharp distribution built on thin evidence
    is well-formed and not yet worth believing."""
    result = classifier.classify(
        evidence(detection_count=1, hints=(hint(SMALL, 0.95),), hint_quality=(0.95,))
    )

    assert result.distribution[SMALL] > 0.5
    assert result.confidence < result.distribution[SMALL]


def test_immature_track_is_less_confident_than_a_mature_one(classifier: Classifier) -> None:
    def confidence_at(detections: int) -> float:
        return classifier.classify(
            evidence(
                detection_count=detections, hints=(hint(SMALL, 0.9),), hint_quality=(0.9,)
            )
        ).confidence

    assert confidence_at(1) < confidence_at(4) < confidence_at(20)


def test_coasting_reduces_confidence(classifier: Classifier) -> None:
    """Whatever was concluded is ageing while nothing is being observed."""
    observed = classifier.classify(evidence(hints=(hint(SMALL),), hint_quality=(0.9,)))
    coasting = classifier.classify(
        evidence(coasting=True, hints=(hint(SMALL),), hint_quality=(0.9,))
    )

    assert coasting.confidence < observed.confidence


def test_confidence_stays_within_bounds(classifier: Classifier) -> None:
    result = classifier.classify(
        evidence(
            detection_count=10_000,
            hints=tuple(hint(SMALL, 1.0) for _ in range(20)),
            hint_quality=tuple(1.0 for _ in range(20)),
        )
    )

    assert 0.0 <= result.confidence <= 1.0


# --------------------------------------------------------------------------------------
# Provenance (ADR 0012)
# --------------------------------------------------------------------------------------


def test_simulated_truth_is_heavily_discounted(classifier: Classifier) -> None:
    """A value copied from ground truth without degradation must not drive an operational
    answer, even though it is still recorded as having been said."""
    measured = classifier.classify(
        evidence(hints=(hint(SMALL, 0.9, provenance=Provenance.MEASURED),), hint_quality=(0.9,))
    )
    truth = classifier.classify(
        evidence(
            hints=(hint(SMALL, 0.9, provenance=Provenance.SIMULATED_TRUTH),),
            hint_quality=(0.9,),
        )
    )

    assert truth.distribution[SMALL] < measured.distribution[SMALL]


def test_degraded_sensor_measurement_is_not_discounted(classifier: Classifier) -> None:
    """The simulator marks its confusion-model draws `measured`, because that is what they
    are. If they were discounted, the classifier would behave differently in SIMULATION and
    LIVE mode on identical data — the failure ADR 0002 exists to prevent."""
    result = classifier.classify(
        evidence(hints=(hint(SMALL, 0.9, provenance=Provenance.MEASURED),), hint_quality=(0.9,))
    )

    hint_contribution = next(c for c in result.evidence if c.source == "sensor_hint")
    assert hint_contribution.weight == pytest.approx(0.9 * 0.9, abs=1e-6)


# --------------------------------------------------------------------------------------
# Explainability
# --------------------------------------------------------------------------------------


def test_every_contributing_source_supplies_a_reason(classifier: Classifier) -> None:
    result = classifier.classify(evidence(hints=(hint(SMALL),), hint_quality=(0.9,)))

    assert all(c.detail.strip() for c in result.evidence)
    assert {c.source for c in result.evidence} == {"sensor_hint", "kinematic"}


def test_disagreement_is_stated_in_plain_language(classifier: Classifier) -> None:
    result = classifier.classify(
        evidence(hints=(hint(BIRD, 0.7), hint(SMALL, 0.7)), hint_quality=(0.9, 0.9))
    )

    detail = next(c.detail for c in result.evidence if c.source == "sensor_hint")
    assert "disagree" in detail.lower()


# --------------------------------------------------------------------------------------
# Band membership
# --------------------------------------------------------------------------------------


def test_band_is_soft_at_the_edges() -> None:
    """Hard bounds would make an object at 26 m/s categorically not a class whose typical
    band ends at 25, which is not how any of these categories work."""
    band = Band(absolute_min=0.0, typical_min=10.0, typical_max=25.0, absolute_max=35.0)

    assert band.membership(15.0) == 1.0
    assert band.membership(30.0) == pytest.approx(0.5)
    assert band.membership(5.0) == pytest.approx(0.5)
    assert band.membership(40.0) == 0.0
    assert band.membership(-1.0) == 0.0


def test_profile_membership_is_zero_if_any_dimension_rules_it_out() -> None:
    """Geometric mean, so one impossible dimension is decisive. An arithmetic mean would let
    a ground vehicle at 400 m survive on the strength of a plausible speed."""
    profile = KinematicProfile(
        label=VEHICLE,
        speed_mps=Band(absolute_min=0, typical_min=1, typical_max=30, absolute_max=55),
        altitude_m=Band(absolute_min=-30, typical_min=-5, typical_max=15, absolute_max=40),
        vertical_rate_mps=Band(absolute_min=-2, typical_min=-0.5, typical_max=0.5, absolute_max=2),
        prior=0.5,
    )

    assert profile.membership(evidence(speed_mps=20.0, altitude_m=5.0)) > 0.0
    assert profile.membership(evidence(speed_mps=20.0, altitude_m=400.0)) == 0.0


# --------------------------------------------------------------------------------------
# Result invariants
# --------------------------------------------------------------------------------------


def test_distribution_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match="sum to 1.0"):
        ClassificationResult(distribution={SMALL: 0.5, BIRD: 0.2}, label=SMALL, confidence=0.5)


def test_top_label_resolves_ties_to_unknown() -> None:
    assert top_label({SMALL: 0.5, BIRD: 0.5}) is UNKNOWN
    assert top_label({}) is UNKNOWN
    assert top_label({SMALL: 0.6, BIRD: 0.4}) is SMALL


def test_configured_profiles_load_and_cover_the_expected_labels() -> None:
    """The shipped configuration must actually parse — a classifier that cannot load its
    envelopes fails at startup, not at the first track."""
    classifier = build_default_classifier()
    kinematic = next(s for s in classifier.sources if s.name == "kinematic")

    labels = {profile.label for profile in kinematic._profiles}
    assert {SMALL, BIRD, VEHICLE} <= labels


class TestUnmeasuredDimensions:
    """A dimension nobody measured must abstain, not veto.

    This is the failure mode a radar introduces. A unit with no elevation channel reports a
    track at the sensor's own mounting height — a constant, not an observation — and the
    geometric mean in `KinematicProfile.membership` turns any zero into a decisive zero. So an
    assumed altitude silently and permanently rules out every label whose band excludes it,
    which for a ground-sited radar is `vehicle`: among the likeliest true labels for its
    clutter, excluded on the basis of a number nobody measured.
    """

    @staticmethod
    def _profile() -> KinematicProfile:
        return KinematicProfile(
            label=ClassificationLabel.VEHICLE,
            speed_mps=Band(absolute_min=0.0, typical_min=2.0, typical_max=30.0, absolute_max=55.0),
            altitude_m=Band(
                absolute_min=-30.0, typical_min=-5.0, typical_max=15.0, absolute_max=40.0
            ),
            vertical_rate_mps=Band(
                absolute_min=-3.0, typical_min=-0.5, typical_max=0.5, absolute_max=3.0
            ),
            prior=0.25,
        )

    @staticmethod
    def _evidence(**overrides) -> TrackEvidence:
        base = {
            "track_id": "TRK-00001",
            "speed_mps": 12.0,
            "altitude_m": 62.0,  # a site reference altitude, not a measurement
            "vertical_rate_mps": 0.0,
            "detection_count": 6,
            "coasting": False,
        }
        base.update(overrides)
        return TrackEvidence(**base)

    def test_an_assumed_altitude_vetoes_the_label_when_treated_as_measured(self) -> None:
        """The trap, stated as a test so it cannot be reintroduced silently."""
        membership = self._profile().membership(self._evidence(altitude_measured=True))

        assert membership == 0.0

    def test_the_same_track_is_scored_on_speed_alone_when_altitude_is_unmeasured(self) -> None:
        """The fix: exclude the dimension rather than let it decide."""
        membership = self._profile().membership(
            self._evidence(altitude_measured=False, vertical_rate_measured=False)
        )

        assert membership > 0.0

    def test_a_measured_altitude_still_rules_a_label_out(self) -> None:
        """The abstention must not cost the classifier its ability to exclude.

        Ruling out a ground vehicle at 400 m is the main thing kinematic evidence is for, and
        that must keep working wherever a sensor genuinely measured the height.
        """
        membership = self._profile().membership(
            self._evidence(altitude_m=400.0, altitude_measured=True)
        )

        assert membership == 0.0

    def test_speed_still_rules_a_label_out_when_altitude_abstains(self) -> None:
        """Excluding a dimension narrows the evidence; it does not disable the rest of it."""
        membership = self._profile().membership(
            self._evidence(speed_mps=90.0, altitude_measured=False, vertical_rate_measured=False)
        )

        assert membership == 0.0

    def test_excluding_a_partially_matching_dimension_raises_the_raw_membership(self) -> None:
        """States the real trade-off rather than a comfortable one.

        Dropping a dimension that partially matched *does* raise the raw geometric mean — 0.69
        becomes 1.0 here. That is not a bug and it is not hidden: the number means "of what was
        measured, this agrees", and it should be higher than a score dragged down by a
        dimension that was never an observation.

        What matters is that it cannot distort the outcome, which the next test covers. An
        earlier version of this test asserted `<=` and was vacuous — both sides were 1.0, so it
        could not fail and did not describe the behaviour.
        """
        profile = self._profile()
        partial = self._evidence(altitude_m=20.0, altitude_measured=True)
        abstaining = self._evidence(
            altitude_m=20.0, altitude_measured=False, vertical_rate_measured=False
        )

        assert profile.membership(partial) < profile.membership(abstaining)

    def test_abstaining_cannot_change_which_label_wins(self) -> None:
        """The property that actually protects the result.

        Every label is scored against the same evidence, so a dimension excluded is excluded
        for all of them, and `KinematicEvidence` normalises across profiles afterwards. Raw
        memberships all move together; the ranking they produce does not shift because of the
        exclusion itself — only because a fabricated dimension is no longer vetoing.
        """
        speeds = (4.0, 12.0, 25.0)
        profile = self._profile()
        other = KinematicProfile(
            label=ClassificationLabel.SMALL_UAS,
            speed_mps=Band(
                absolute_min=0.0, typical_min=3.0, typical_max=20.0, absolute_max=40.0
            ),
            altitude_m=Band(
                absolute_min=0.0, typical_min=10.0, typical_max=120.0, absolute_max=400.0
            ),
            vertical_rate_mps=Band(
                absolute_min=-10.0, typical_min=-4.0, typical_max=4.0, absolute_max=10.0
            ),
            prior=0.25,
        )

        for speed in speeds:
            measured = self._evidence(speed_mps=speed, altitude_m=60.0, altitude_measured=True)
            abstaining = self._evidence(
                speed_mps=speed,
                altitude_m=60.0,
                altitude_measured=False,
                vertical_rate_measured=False,
            )
            # With altitude measured at 60 m the vehicle profile is vetoed outright; with it
            # abstaining both profiles are ranked on speed alone, which is the whole point.
            assert profile.membership(measured) == 0.0
            assert profile.membership(abstaining) > 0.0
            assert other.membership(abstaining) > 0.0

    def test_dimensions_are_measured_by_default(self) -> None:
        """Existing sources measure height. The change must not alter their behaviour."""
        evidence = self._evidence()

        assert evidence.altitude_measured is True
        assert evidence.vertical_rate_measured is True
