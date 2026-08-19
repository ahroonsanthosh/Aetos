"""Ingestion: the boundary guarantees — record before act, deduplicate, reject loudly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.common.clock import SimClock
from backend.events.store import SqliteEventStore
from backend.events.types import EventType
from backend.ingestion.pipeline import IngestionPipeline
from backend.zones.geometry import load_site_geometry
from sensors.schemas.detection import (
    ClassificationHint,
    ClassificationLabel,
    Detection,
    GeoPosition,
    Measurement,
    Provenance,
    Quality,
    SensorRef,
    SensorType,
)

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> SimClock:
    return SimClock(START)


@pytest.fixture
def events(clock: SimClock) -> SqliteEventStore:
    store = SqliteEventStore(clock)
    yield store
    store.close()


@pytest.fixture(scope="module")
def geometry():
    return load_site_geometry("fictional_test_range")


@pytest.fixture
def pipeline(clock: SimClock, events: SqliteEventStore, geometry) -> IngestionPipeline:
    return IngestionPipeline(clock, events, geometry)


def detection(detection_id: str = "det-1", *, timestamp=None, classification=None) -> Detection:
    site = load_site_geometry("fictional_test_range").site
    return Detection(
        detection_id=detection_id,
        timestamp=timestamp or START,
        sensor=SensorRef(sensor_id="radar_north", sensor_type=SensorType.SIMULATED_RADAR),
        position=GeoPosition(
            latitude=site.reference.latitude + 0.01,
            longitude=site.reference.longitude,
            altitude_m=120.0,
        ),
        measurement=Measurement(position_uncertainty_m=15.0),
        quality=Quality(confidence=0.87),
        classification=classification,
    )


def test_accepted_detection_is_recorded_before_it_is_reduced(
    pipeline: IngestionPipeline, events: SqliteEventStore
) -> None:
    """ADR 0006: the log contains the detection before anything acts on it. This is what
    makes GET /detections real rather than an endpoint over an empty table."""
    result = pipeline.ingest([detection()])

    recorded = [e for e in events.read_from(1) if e.event_type is EventType.DETECTION_RECEIVED]
    assert len(recorded) == 1
    assert result.accepted == 1
    assert len(result.for_tracking) == 1


def test_reduction_drops_classification_from_the_tracker_input(
    pipeline: IngestionPipeline,
) -> None:
    """ADR 0002: the tracker must be structurally incapable of consuming a classification
    hint, not merely trusted not to look at one."""
    result = pipeline.ingest(
        [
            detection(
                classification=ClassificationHint(
                    label=ClassificationLabel.SMALL_UAS,
                    confidence=0.9,
                    provenance=Provenance.MEASURED,
                )
            )
        ]
    )

    incoming = result.for_tracking[0]
    assert not hasattr(incoming, "classification")
    assert not hasattr(incoming, "speed_mps")


def test_classification_hint_is_still_returned_for_the_classifier(
    pipeline: IngestionPipeline,
) -> None:
    """Dropped from the tracker's input, kept as evidence about what the object is. That
    distinction is the whole point of ADR 0002 — it forbids influence on *where*, not use
    as evidence for *what*."""
    result = pipeline.ingest(
        [
            detection(
                classification=ClassificationHint(
                    label=ClassificationLabel.SMALL_UAS,
                    confidence=0.9,
                    provenance=Provenance.MEASURED,
                )
            )
        ]
    )

    assert result.hints["det-1"].label is ClassificationLabel.SMALL_UAS
    assert result.quality["det-1"] == pytest.approx(0.87)


def test_detection_without_a_hint_contributes_none(pipeline: IngestionPipeline) -> None:
    """Most sensors do not classify; a radar profile reports nothing at all."""
    result = pipeline.ingest([detection()])

    assert result.hints == {}


def test_duplicate_detection_id_is_suppressed(
    pipeline: IngestionPipeline, events: SqliteEventStore
) -> None:
    """FM-013. A repeat reaching the filter would double-weight one observation and make the
    estimate falsely confident."""
    pipeline.ingest([detection("det-1")])
    result = pipeline.ingest([detection("det-1")])

    assert result.duplicates == 1
    assert result.for_tracking == []

    received = [e for e in events.read_from(1) if e.event_type is EventType.DETECTION_RECEIVED]
    assert len(received) == 1


def test_duplicate_is_recorded_rather_than_dropped_silently(
    pipeline: IngestionPipeline, events: SqliteEventStore
) -> None:
    pipeline.ingest([detection("det-1")])
    pipeline.ingest([detection("det-1")])

    rejected = [e for e in events.read_from(1) if e.event_type is EventType.DETECTION_REJECTED]
    assert len(rejected) == 1
    assert rejected[0].payload["reason"] == "duplicate_detection_id"


def test_detection_from_the_future_is_rejected(
    pipeline: IngestionPipeline, events: SqliteEventStore
) -> None:
    """FM-012. A timestamp ahead of now cannot be a late arrival, so it is a clock problem."""
    result = pipeline.ingest([detection(timestamp=START + timedelta(minutes=5))])

    assert result.rejected == 1
    assert result.for_tracking == []
    rejected = [e for e in events.read_from(1) if e.event_type is EventType.DETECTION_REJECTED]
    assert rejected[0].payload["reason"] == "timestamp_in_future"


def test_stale_detection_is_rejected(pipeline: IngestionPipeline) -> None:
    result = pipeline.ingest([detection(timestamp=START - timedelta(minutes=5))])

    assert result.rejected == 1


def test_detection_within_the_skew_window_is_accepted(pipeline: IngestionPipeline) -> None:
    """Real networks deliver late. Only implausible skew is rejected, not any skew at all."""
    result = pipeline.ingest([detection(timestamp=START - timedelta(seconds=5))])

    assert result.accepted == 1


def test_one_bad_detection_does_not_stop_the_others(pipeline: IngestionPipeline) -> None:
    """FM-011: one misbehaving sensor must not take the pipeline down with it."""
    result = pipeline.ingest(
        [
            detection("det-good-1"),
            detection("det-bad", timestamp=START + timedelta(hours=1)),
            detection("det-good-2"),
        ]
    )

    assert result.accepted == 2
    assert result.rejected == 1
    assert len(result.for_tracking) == 2


def test_dedupe_window_is_bounded(clock: SimClock, events: SqliteEventStore, geometry) -> None:
    """A long-running system must not accumulate one remembered id per detection forever."""
    pipeline = IngestionPipeline(clock, events, geometry, dedupe_window=3)

    for i in range(5):
        pipeline.ingest([detection(f"det-{i}")])

    # The oldest ids have been evicted, so they no longer suppress a repeat.
    assert pipeline.ingest([detection("det-0")]).accepted == 1
    # The most recent ones are still remembered.
    assert pipeline.ingest([detection("det-4")]).duplicates == 1


def test_position_is_converted_to_the_site_frame(pipeline: IngestionPipeline) -> None:
    """Tracking works in local ENU metres (ADR 0004); ingestion is where the conversion
    happens, once, at the boundary."""
    result = pipeline.ingest([detection()])

    enu = result.for_tracking[0].position_enu
    assert enu.shape == (3,)
    # 0.01 degrees north of the reference is roughly a kilometre.
    assert 900.0 < enu[1] < 1200.0


def test_scalar_uncertainty_becomes_an_isotropic_covariance(
    pipeline: IngestionPipeline,
) -> None:
    result = pipeline.ingest([detection()])

    covariance = result.for_tracking[0].covariance
    assert covariance.shape == (3, 3)
    assert covariance[0][0] == pytest.approx(15.0**2)


# --------------------------------------------------------------------------------------
# Uncertainty a filter cannot use is malformed input, not a precise measurement
# --------------------------------------------------------------------------------------


def _detection_with(measurement: Measurement, clock: SimClock) -> Detection:
    return Detection(
        detection_id="DET-UNC-1",
        timestamp=clock.now(),
        sensor=SensorRef(sensor_id="radar_north", sensor_type=SensorType.SIMULATED_RADAR),
        position=GeoPosition(latitude=51.9000, longitude=-8.4697, altitude_m=120.0),
        measurement=measurement,
        quality=Quality(confidence=0.8),
    )


@pytest.mark.parametrize(
    ("label", "measurement"),
    [
        ("zero scalar uncertainty", Measurement(position_uncertainty_m=0.0)),
        (
            "all-zero covariance",
            Measurement(
                position_uncertainty_m=5.0,
                position_covariance_enu=[[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
            ),
        ),
        (
            "singular covariance with positive variances",
            Measurement(
                position_uncertainty_m=5.0,
                position_covariance_enu=[[4.0, 4.0, 0.0], [4.0, 4.0, 0.0], [0.0, 0.0, 4.0]],
            ),
        ),
    ],
)
def test_degenerate_uncertainty_is_rejected_at_the_boundary(
    label: str, measurement: Measurement, pipeline: IngestionPipeline, clock: SimClock
) -> None:
    """Each of these is schema-legal and none of them can be tracked.

    `position_uncertainty_m` is `ge=0.0` and the covariance validator only rejects negative
    variances, so a sensor can assert perfect or degenerate positional knowledge and pass
    validation. The filter cannot survive it: the innovation covariance is singular and both
    the association gate and the update raise `LinAlgError`, taking down the entire tracking
    cycle rather than the one detection that caused it.

    A sensor claiming it knows exactly where something is has not made a precise measurement,
    it has made a broken one (FM-011).
    """
    result = pipeline.ingest([_detection_with(measurement, clock)])

    assert result.accepted == 0, f"{label} was accepted and would have reached the filter"
    assert result.rejected == 1


def test_a_singular_report_costs_only_its_own_detection(
    pipeline: IngestionPipeline, clock: SimClock
) -> None:
    """The point of rejecting at the boundary: one malformed sensor must not cost the sky."""
    good = _detection_with(Measurement(position_uncertainty_m=8.0), clock).model_copy(
        update={"detection_id": "DET-GOOD"}
    )
    bad = _detection_with(Measurement(position_uncertainty_m=0.0), clock)

    result = pipeline.ingest([bad, good])

    assert result.accepted == 1
    assert result.rejected == 1
    assert [d.detection_id for d in result.for_tracking] == ["DET-GOOD"]


def test_realistic_uncertainty_still_passes(pipeline: IngestionPipeline, clock: SimClock) -> None:
    """The guard must not be so eager that it rejects a genuinely precise sensor. A
    centimetre-class report is implausible for radar and must still be tracked."""
    result = pipeline.ingest([_detection_with(Measurement(position_uncertainty_m=0.01), clock)])

    assert result.accepted == 1
