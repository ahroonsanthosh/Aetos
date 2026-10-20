"""Fusion: several sensors seeing one object must produce one object.

The asymmetry to keep in mind throughout. Failing to fuse produces an over-count, which the
operator can see and which the situation assessment already reports. Fusing wrongly produces an
under-count — two real contacts shown as one — which nobody can see, because the display looks
exactly as it would if there really were one. Every default here leans towards the visible
error, and several tests exist specifically to pin that lean in place.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from backend.fusion.policy import FusionPolicy
from backend.fusion.reconcile import MULTI_SENSOR_MARKER, DetectionReconciler
from backend.tracking.track_manager.manager import IncomingDetection

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def detection(
    detection_id: str,
    *,
    sensor_id: str,
    east_m: float = 0.0,
    north_m: float = 1000.0,
    up_m: float = 120.0,
    uncertainty_m: float = 15.0,
    at: datetime = START,
) -> IncomingDetection:
    return IncomingDetection(
        detection_id=detection_id,
        sensor_id=sensor_id,
        timestamp=at,
        position_enu=np.array([east_m, north_m, up_m]),
        covariance=np.eye(3) * (uncertainty_m**2),
    )


@pytest.fixture
def reconciler() -> DetectionReconciler:
    return DetectionReconciler()


def test_three_sensors_watching_one_drone_produce_one_measurement(
    reconciler: DetectionReconciler,
) -> None:
    """FM-023 itself, in miniature.

    The observed failure was one drone appearing as three concurrent CONFIRMED tracks because
    each sensor's detection of it started its own. This is that input.
    """
    result = reconciler.reconcile(
        [
            detection("det-n", sensor_id="radar_north", east_m=0.0, north_m=1000.0),
            detection("det-w", sensor_id="radar_west", east_m=12.0, north_m=1006.0),
            detection("det-s", sensor_id="radar_south", east_m=-9.0, north_m=994.0),
        ]
    )

    assert len(result.detections) == 1
    assert result.reconciled == 2

    fused = result.detections[0]
    assert fused.is_multi_sensor
    assert set(fused.contributing_sensor_ids) == {"radar_north", "radar_west", "radar_south"}
    assert set(fused.contributing_detection_ids) == {"det-n", "det-w", "det-s"}


def test_two_genuinely_separate_contacts_are_not_merged(
    reconciler: DetectionReconciler,
) -> None:
    """The failure that would be invisible, and therefore the one worth most.

    Two drones a kilometre apart, each seen by two sensors. The right answer is two objects; the
    dangerous wrong answer is one, because an operator shown one contact where there are two has
    been told the sky is emptier than it is and has no way to notice.
    """
    result = reconciler.reconcile(
        [
            detection("det-a-n", sensor_id="radar_north", east_m=0.0, north_m=1000.0),
            detection("det-a-w", sensor_id="radar_west", east_m=10.0, north_m=1004.0),
            detection("det-b-n", sensor_id="radar_north", east_m=0.0, north_m=2000.0),
            detection("det-b-w", sensor_id="radar_west", east_m=8.0, north_m=2005.0),
        ]
    )

    assert len(result.detections) == 2
    assert all(fused.is_multi_sensor for fused in result.detections)

    norths = sorted(float(fused.position_enu[1]) for fused in result.detections)
    assert norths[0] == pytest.approx(1002.0, abs=10.0)
    assert norths[1] == pytest.approx(2002.0, abs=10.0)


def test_two_returns_from_one_sensor_are_never_fused(reconciler: DetectionReconciler) -> None:
    """The structural rule, and the reason single-linkage clustering is safe here.

    A sensor reports each object once per scan, so two returns from one sensor in one cycle are
    two objects — or a duplicate, and duplicates are already gone by `detection_id` at ingestion
    (FM-013). Fusing them would mean concluding one sensor saw one object twice, which the input
    cannot mean. They are placed close enough together that the statistical gate alone would
    happily merge them.
    """
    result = reconciler.reconcile(
        [
            detection("det-1", sensor_id="radar_north", east_m=0.0, north_m=1000.0),
            detection("det-2", sensor_id="radar_north", east_m=3.0, north_m=1002.0),
        ]
    )

    assert len(result.detections) == 2
    assert result.reconciled == 0


def test_a_wide_covariance_cannot_swallow_a_separate_contact(
    reconciler: DetectionReconciler,
) -> None:
    """The hard ceiling, and why the statistical gate is not sufficient on its own.

    A sensor reporting enormous uncertainty produces a gate wide enough to admit almost
    anything, so without a ceiling fusion would be most aggressive exactly where the
    measurements are least trustworthy — merging real contacts on the strength of a
    measurement that admits it does not know where they are.
    """
    result = reconciler.reconcile(
        [
            detection("det-vague", sensor_id="radar_north", north_m=1000.0, uncertainty_m=5000.0),
            detection("det-real", sensor_id="radar_west", north_m=1600.0, uncertainty_m=5000.0),
        ]
    )

    assert len(result.detections) == 2, (
        "a sensor claiming huge uncertainty merged two contacts 600 m apart"
    )


def test_fusing_tightens_the_estimate_rather_than_averaging_it_away(
    reconciler: DetectionReconciler,
) -> None:
    """Information adds. Three independent looks at one object genuinely do locate it better.

    This is the gain that makes measurement fusion worth doing rather than merely de-duplicating:
    the filter receives one better-conditioned observation instead of the best of three.
    """
    single = detection("det-n", sensor_id="radar_north", uncertainty_m=15.0)
    result = reconciler.reconcile(
        [
            single,
            detection("det-w", sensor_id="radar_west", east_m=4.0, uncertainty_m=15.0),
            detection("det-s", sensor_id="radar_south", east_m=-4.0, uncertainty_m=15.0),
        ]
    )

    fused = result.detections[0]
    assert np.trace(fused.covariance) < np.trace(single.covariance)


def test_a_confident_sensor_pulls_the_estimate_more_than_a_vague_one(
    reconciler: DetectionReconciler,
) -> None:
    """Inverse-covariance weighting, asserted as behaviour rather than as an implementation.

    An equal-vote average would let the worst sensor on the site drag the position of every
    object it can see, which is the opposite of what having a good sensor is for.
    """
    result = reconciler.reconcile(
        [
            detection("det-precise", sensor_id="radar_north", north_m=1000.0, uncertainty_m=5.0),
            detection("det-vague", sensor_id="radar_west", north_m=1100.0, uncertainty_m=80.0),
        ]
    )

    fused = result.detections[0]
    assert fused.is_multi_sensor
    north = float(fused.position_enu[1])
    assert north < 1050.0, "the fused position sat at or beyond the midpoint"
    assert north == pytest.approx(1000.0, abs=10.0)


def test_a_single_sensor_site_is_untouched(reconciler: DetectionReconciler) -> None:
    """Fusing one detection must be indistinguishable from not fusing at all.

    A site with one sensor cannot exhibit FM-023, and its tracking must be bit-identical whether
    this module runs or not — otherwise enabling fusion would change the behaviour of every
    deployment that has nothing to reconcile.
    """
    original = detection("det-1", sensor_id="radar_north")
    result = reconciler.reconcile([original])

    fused = result.detections[0]
    assert fused.detection_id == original.detection_id
    assert fused.sensor_id == original.sensor_id
    assert not fused.is_multi_sensor
    assert np.array_equal(fused.position_enu, original.position_enu)
    assert np.array_equal(fused.covariance, original.covariance)
    assert result.reconciled == 0


def test_disabling_the_policy_passes_everything_through(reconciler: DetectionReconciler) -> None:
    disabled = DetectionReconciler(FusionPolicy(enabled=False))
    detections = [
        detection("det-n", sensor_id="radar_north", east_m=0.0),
        detection("det-w", sensor_id="radar_west", east_m=5.0),
    ]

    assert len(disabled.reconcile(detections).detections) == 2
    assert len(reconciler.reconcile(detections).detections) == 1


def test_the_fused_identifier_names_its_parts(reconciler: DetectionReconciler) -> None:
    """A measurement that appeared from nowhere would be untraceable in the log."""
    result = reconciler.reconcile(
        [
            detection("det-n", sensor_id="radar_north"),
            detection("det-w", sensor_id="radar_west", east_m=4.0),
        ]
    )

    fused_id = result.detections[0].detection_id
    assert fused_id.startswith(f"{MULTI_SENSOR_MARKER}:")
    assert "det-n" in fused_id and "det-w" in fused_id


def test_reconciliation_is_order_stable(reconciler: DetectionReconciler) -> None:
    """A replayed scenario must reconcile identically (ADR 0008 §1)."""
    detections = [
        detection("det-a-n", sensor_id="radar_north", north_m=1000.0),
        detection("det-b-n", sensor_id="radar_north", north_m=3000.0),
        detection("det-a-w", sensor_id="radar_west", north_m=1004.0),
        detection("det-b-w", sensor_id="radar_west", north_m=3003.0),
    ]

    first = reconciler.reconcile(detections)
    second = reconciler.reconcile(list(detections))

    assert [f.detection_id for f in first.detections] == [f.detection_id for f in second.detections]
    assert len(first.detections) == 2


def test_a_chain_of_detections_does_not_collapse_into_one_object(
    reconciler: DetectionReconciler,
) -> None:
    """Single-linkage clustering's classic failure, guarded explicitly.

    Each detection here is within gate distance of the next, but the ends are far apart. Naive
    chaining would report one object spanning the whole line. Requiring every existing member to
    gate against a candidate — rather than just the nearest — is what prevents it.
    """
    result = reconciler.reconcile(
        [
            detection("det-1", sensor_id="radar_north", north_m=1000.0),
            detection("det-2", sensor_id="radar_west", north_m=1030.0),
            detection("det-3", sensor_id="radar_south", north_m=1060.0),
            detection("det-4", sensor_id="radar_east", north_m=1090.0),
        ]
    )

    assert len(result.detections) > 1, "a chain of detections collapsed into a single object"


def test_a_detection_that_could_belong_to_two_objects_is_fused_into_neither(
    reconciler: DetectionReconciler,
) -> None:
    """Ambiguity rejection: the mechanism that makes a crossing safe.

    Three sensors. Two of the detections are far enough apart to be separate objects; the third
    sits between them, close enough to gate against both. There is nothing in one cycle of
    positions that says which of the two it belongs to, so it is fused into neither.

    Guessing would be the invisible failure. Assigning it to whichever cluster happened to be
    tried first would produce a confident two-object picture built on a coin flip, and — because
    fusion runs every cycle — the coin would land differently as the geometry shifted, so an
    operator would see contacts merging and splitting with no change in the sky to explain it.

    Separating objects this close is the tracker's job, not fusion's: it carries velocity state
    and can tell two crossing contacts apart (ADR 0005), and fusion cannot.
    """
    # Chosen against the gate arithmetic rather than by eye: with sigma 15 m the combined
    # covariance is 450, so the widest gating separation is sqrt(11.345 * 450) ~= 71 m.
    result = reconciler.reconcile(
        [
            detection("det-a", sensor_id="radar_north", north_m=1000.0),
            detection("det-b", sensor_id="radar_south", north_m=1140.0),
            detection("det-ambiguous", sensor_id="radar_east", north_m=1070.0),
        ]
    )

    assert len(result.detections) == 3, (
        "a detection that gates against two separate clusters was fused into one of them; "
        "fusion is meant to decline where the evidence does not choose"
    )
    assert all(not fused.is_multi_sensor for fused in result.detections)


# --- safeguards added when fusion was ported onto live mode --------------------------------


def test_a_real_detection_is_never_fused_with_a_simulated_one() -> None:
    """HYBRID mode feeds real and synthetic detections into one cycle.

    Averaging a radar return with a synthetic one would produce a measurement that is partly
    invented while carrying a real sensor's name — the LIVE-badge-over-synthetic-data failure
    (FM-053), one stage further in. Both detections here sit on top of each other, so nothing
    but the provenance rule keeps them apart.
    """
    kinds = {"radar-real": "real", "radar-sim": "simulated"}
    reconciler = DetectionReconciler(provenance=kinds.get)

    result = reconciler.reconcile(
        [detection("a", sensor_id="radar-real"), detection("b", sensor_id="radar-sim")]
    )

    assert result.reconciled == 0
    assert [d.contributing_sensor_ids for d in result.detections] == [
        ("radar-real",),
        ("radar-sim",),
    ]


def test_two_real_sensors_still_fuse_under_the_provenance_rule() -> None:
    """The rule separates kinds; it must not stop the fusion it exists alongside."""
    kinds = {"radar-1": "real", "radar-2": "real"}
    reconciler = DetectionReconciler(provenance=kinds.get)

    result = reconciler.reconcile(
        [detection("a", sensor_id="radar-1"), detection("b", sensor_id="radar-2")]
    )

    assert result.reconciled == 1


def test_a_sensor_of_unknown_provenance_is_fused_with_nothing() -> None:
    """A detection from a sensor nothing attached claims is not evidence of either kind, so it
    stands alone — an over-count, the visible direction."""
    kinds = {"radar-1": "real"}
    reconciler = DetectionReconciler(provenance=kinds.get)

    result = reconciler.reconcile(
        [detection("a", sensor_id="radar-1"), detection("b", sensor_id="who-knows")]
    )

    assert result.reconciled == 0


def test_the_gate_is_the_one_it_is_given_not_a_copy() -> None:
    """Fusion judges "same object" on the tracker's gate. A tighter gate handed in must refuse a
    pair the default admits — proving the value used is the one passed, not a second constant."""
    near_pair = [
        detection("a", sensor_id="radar-1", east_m=0.0),
        detection("b", sensor_id="radar-2", east_m=40.0),
    ]

    assert DetectionReconciler().reconcile(near_pair).reconciled == 1
    assert DetectionReconciler(gate_threshold=0.5).reconcile(near_pair).reconciled == 0


def test_a_fused_measurement_carries_its_real_sensors_to_the_tracker() -> None:
    """The track records the radars that saw the object, not the "fused" marker."""
    from backend.fusion.reconcile import to_incoming

    result = DetectionReconciler().reconcile(
        [detection("a", sensor_id="radar-1"), detection("b", sensor_id="radar-2")]
    )
    incoming = to_incoming(result.detections[0])

    assert incoming.sensor_id == MULTI_SENSOR_MARKER
    assert incoming.sensors == ("radar-1", "radar-2")


def test_an_unfused_detection_reaches_the_tracker_unchanged() -> None:
    from backend.fusion.reconcile import to_incoming

    original = detection("a", sensor_id="radar-1")
    (passed,) = DetectionReconciler().reconcile([original]).detections
    incoming = to_incoming(passed)

    assert incoming.detection_id == original.detection_id
    assert incoming.sensor_id == original.sensor_id
    assert incoming.timestamp == original.timestamp
    assert np.array_equal(incoming.position_enu, original.position_enu)
    assert np.array_equal(incoming.covariance, original.covariance)
    assert incoming.contributing_sensor_ids == ()


def test_a_missing_policy_file_is_refused_not_defaulted(tmp_path) -> None:
    """Same contract as every other policy loader: defaults applied invisibly are discovered
    during an incident."""
    from backend.common.config import ConfigError, ConfigPaths
    from backend.fusion.policy import load_fusion_policy

    paths = ConfigPaths(repo_root=tmp_path, config_root=tmp_path / "config")
    with pytest.raises(ConfigError, match="fusion policy not found"):
        load_fusion_policy(paths)


def test_the_shipped_policy_loads() -> None:
    from backend.fusion.policy import load_fusion_policy

    assert load_fusion_policy().enabled
