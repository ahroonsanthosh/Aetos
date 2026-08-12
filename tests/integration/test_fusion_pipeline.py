"""Fusion against the real pipeline, on the scenario where FM-023 was actually observed.

The unit tests construct detections to order. These run the whole thing — real scenario, real
simulator, real noise, real sensor models — because FM-023 was found by watching a live run and
not by reasoning, and a fix for it should be confirmed the same way.

Both directions are asserted here. The first tests that fusion does what it was built for. The
rest test that it does not do the thing that would be worse, and one of them documents a limit
it does not clear.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from backend.classification.service import CycleEvidence
from backend.common.clock import SimClock
from backend.fusion.policy import FusionPolicy
from backend.fusion.reconcile import DetectionReconciler, to_incoming
from backend.main import Pipeline
from backend.tracking.models import TrackState
from backend.zones.geometry import SiteGeometry, load_site_geometry
from scenarios.library import load_all
from scenarios.world import ScenarioWorld
from sensors.schemas.detection import ClassificationHint, ClassificationLabel

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
CYCLES = 60
TICK_S = 0.5


@pytest.fixture(scope="module")
def geometry() -> SiteGeometry:
    return load_site_geometry("fictional_test_range")


def confirmed_after_run(
    scenario: str, geometry: SiteGeometry, data_dir: Path, *, fusion: bool, cycles: int = CYCLES
) -> int:
    """Run a scenario end to end and count the CONFIRMED tracks it leaves behind."""
    clock = SimClock(START)
    pipeline = Pipeline(clock, geometry, data_dir)
    pipeline.fusion = DetectionReconciler(FusionPolicy(enabled=fusion))
    try:
        pipeline.attach_scenario(scenario)
        for _ in range(cycles):
            clock.advance(TICK_S)
            pipeline.step()
        return len([
            track
            for track in pipeline.tracker.active_tracks()
            if track.state is TrackState.CONFIRMED
        ])
    finally:
        pipeline.close()


def test_one_drone_seen_by_three_sensors_becomes_one_track(
    geometry: SiteGeometry, tmp_path: Path
) -> None:
    """FM-023, on the scenario it was observed on, with the before and after in one test.

    The recorded symptom was one drone appearing as three concurrent CONFIRMED tracks. The
    comparison is run rather than hardcoded because the point is not the number three — it is
    that fusion is what changes it, and a test asserting only the fused count would keep passing
    if the over-count quietly disappeared for some unrelated reason.
    """
    without = confirmed_after_run(
        "b_approaching_threat", geometry, tmp_path / "without", fusion=False
    )
    with_fusion = confirmed_after_run(
        "b_approaching_threat", geometry, tmp_path / "with", fusion=True
    )

    assert len(load_all()["b_approaching_threat"].objects) == 1
    assert without > 1, "the over-count this test exists for did not reproduce"
    assert with_fusion == 1, f"one drone still produced {with_fusion} confirmed tracks"


def test_fusion_reduces_rather_than_inflates_the_count_where_objects_cross(
    geometry: SiteGeometry, tmp_path: Path
) -> None:
    """Scenario H, which is the one that could be made worse rather than better.

    Two objects converge and cross. Fusion must still help here, or at minimum not hurt — a
    reconciliation stage that improved the easy case and degraded the hard one would be a poor
    trade, because the hard case is the one an operator is actually relying on.
    """
    without = confirmed_after_run("h_track_crossing", geometry, tmp_path / "without", fusion=False)
    with_fusion = confirmed_after_run("h_track_crossing", geometry, tmp_path / "with", fusion=True)

    assert with_fusion <= without


def test_two_objects_closer_than_the_sensor_noise_may_transiently_merge(
    geometry: SiteGeometry, tmp_path: Path
) -> None:
    """A limitation, asserted so that it is a known quantity rather than a surprise.

    In `h_track_crossing` the two objects pass within about 60 m of each other, while the site's
    radars report positional sigma of roughly 45-75 m. Two objects separated by less than one
    standard deviation of the measurement noise are not distinguishable *by position alone* —
    that is an information limit, not an implementation defect, and no gate tuning clears it.

    What separates crossing contacts is velocity, which is exactly why the tracker holds filter
    state and association uses it (ADR 0005). Fusion sees one cycle of positions and has no such
    evidence, so where the two overlap it declines to choose (`_cluster`'s ambiguity rejection)
    and otherwise may merge.

    This test measures how often that happens rather than asserting it never does, so that a
    change making it substantially worse fails, and so the number in the README stays honest.
    """
    scenario = load_all()["h_track_crossing"]
    clock = SimClock(START)
    pipeline = Pipeline(clock, geometry, tmp_path / "crossing")
    world = ScenarioWorld(scenario, clock.now())

    merged_across_objects = 0
    multi_sensor_groups = 0
    try:
        pipeline.attach_scenario("h_track_crossing")
        for _ in range(200):
            clock.advance(TICK_S)
            truth = world.objects_at(clock.now())

            detections = []
            for adapter in pipeline._adapters:
                detections.extend(adapter.get_detections())
            ingested = pipeline.ingestion.ingest(detections)
            result = pipeline.fusion.reconcile(ingested.for_tracking)

            if len(truth) >= 2:
                by_id = {d.detection_id: d for d in ingested.for_tracking}
                positions = [
                    np.array([o.position.east_m, o.position.north_m, o.position.up_m])
                    for o in truth
                ]
                for fused in result.detections:
                    if len(fused.contributing_detection_ids) < 2:
                        continue
                    multi_sensor_groups += 1
                    owners = {
                        int(
                            np.argmin(
                                [
                                    np.linalg.norm(by_id[d].position_enu - position)
                                    for position in positions
                                ]
                            )
                        )
                        for d in fused.contributing_detection_ids
                    }
                    if len(owners) > 1:
                        merged_across_objects += 1

            pipeline.tracker.process([to_incoming(f) for f in result.detections])
    finally:
        pipeline.close()

    assert multi_sensor_groups > 0, "fusion never combined anything; the test proves nothing"

    # Measured at roughly 1.6% when this was written. The bound is deliberately loose — it is
    # here to catch a regression that makes merging routine, not to pin a simulator's exact
    # noise draw, which would make this test fail on any unrelated change to the scenario.
    rate = merged_across_objects / multi_sensor_groups
    assert rate < 0.10, (
        f"fusion merged detections of two different objects in {rate:.1%} of multi-sensor "
        "groups; it is meant to decline where the objects are ambiguous, not guess"
    )


def test_classification_hints_survive_fusion(geometry: SiteGeometry, tmp_path: Path) -> None:
    """FM-024, and the regression that matters most here.

    A fused measurement carries a synthesised identifier (`fused:det-a+det-b`), which is not a
    key in ingestion's hint map — those are keyed by the *original* detection ids. So routing
    hints by the assignment alone would find nothing for any fused track, and classification
    would receive an empty evidence pool for exactly the objects it is best placed to identify.
    Every multi-sensor track would sit at `unknown` forever, which is worse than the one-hint
    dilution FM-024 describes.

    `CycleEvidence.from_fused_assignments` maps the fused identifier back to its parts. This
    asserts the hints arrive.
    """
    clock = SimClock(START)
    pipeline = Pipeline(clock, geometry, tmp_path / "hints")
    try:
        pipeline.attach_scenario("b_approaching_threat")

        cycles_with_evidence = 0
        cycles_with_fused_track = 0
        for _ in range(CYCLES):
            clock.advance(TICK_S)

            detections = []
            for adapter in pipeline._adapters:
                detections.extend(adapter.get_detections())
            ingested = pipeline.ingestion.ingest(detections)
            result = pipeline.fusion.reconcile(ingested.for_tracking)
            tracks = pipeline.tracker.process([to_incoming(f) for f in result.detections])

            contributions = {
                f.detection_id: f.contributing_detection_ids for f in result.detections
            }
            evidence = CycleEvidence.from_fused_assignments(
                pipeline.tracker.last_assignments,
                contributions,
                ingested.hints,
                ingested.quality,
            )

            assigned = set(pipeline.tracker.last_assignments.values())
            if any(len(contributions.get(d, ())) > 1 for d in assigned):
                cycles_with_fused_track += 1
                if evidence.hints:
                    cycles_with_evidence += 1

            pipeline.classification.classify_cycle(tracks, evidence)

        assert cycles_with_fused_track > 0, (
            "no track was ever fed a fused measurement, so this proves nothing about fusion"
        )
        assert cycles_with_evidence > 0, (
            "tracks built from fused measurements received no classification hints at all; "
            "the fused identifier is not being mapped back to its contributing detections"
        )
    finally:
        pipeline.close()


def test_fused_contributions_pool_every_sensor_that_reported_an_opinion() -> None:
    """The FM-024 routing itself, at the level where it is deterministic.

    The shipped site has only two classifying sensors — the radars report no classification at
    all — and they do not co-observe on the demo scenario, so a live run cannot demonstrate the
    multi-hint case. It is the routing that has to be right, and that is testable directly.
    """
    hints = {
        "det-rf": ClassificationHint(label=ClassificationLabel.SMALL_UAS, confidence=0.7),
        "det-eo": ClassificationHint(label=ClassificationLabel.SMALL_UAS, confidence=0.86),
    }
    evidence = CycleEvidence.from_fused_assignments(
        {"TRK-00001": "fused:det-rf+det-eo"},
        {"fused:det-rf+det-eo": ("det-rf", "det-eo")},
        hints,
        {"det-rf": 0.8, "det-eo": 0.9},
    )

    assert len(evidence.hints["TRK-00001"]) == 2, (
        "a track observed by two classifying sensors was given one sensor's opinion"
    )
    assert len(evidence.quality["TRK-00001"]) == 2


def test_an_unfused_detection_still_routes_its_hint() -> None:
    """A single-sensor site must be unaffected by the fused routing path."""
    evidence = CycleEvidence.from_fused_assignments(
        {"TRK-00001": "det-solo"},
        {},
        {"det-solo": ClassificationHint(label=ClassificationLabel.BIRD, confidence=0.6)},
        {"det-solo": 0.6},
    )

    assert len(evidence.hints["TRK-00001"]) == 1
