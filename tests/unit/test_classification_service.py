"""Classification as it runs against live tracks: write-back, event emission, and change
detection.

The classifier itself is tested in `test_classification.py`. What is tested here is the
seam between classification and tracking — and it exists because that seam had a defect the
classifier's own tests could never have caught. Classification computed the right answer and
recorded it in the log, but wrote it only into a local copy of the track. The next cycle's
`TRACK_UPDATED`, emitted by the component that actually owns track state, carried a snapshot
with no classification in it and reverted the track to `unknown`. Every unit test passed,
the event counts looked healthy, and the API served `unknown` for confidently labelled
tracks.

The lesson is in the first test below: assert on what a consumer would read, not on what was
written.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from backend.classification.classifier import build_default_classifier
from backend.classification.service import (
    _HINT_MEMORY,
    ClassificationService,
    CycleEvidence,
)
from backend.common.clock import SimClock
from backend.common.geodesy import GeodeticPoint, LocalFrame
from backend.events.store import SqliteEventStore
from backend.events.types import EventType
from backend.tracking.filters.kalman import isotropic_covariance
from backend.tracking.track_manager.manager import (
    TRACK_SNAPSHOT_KEY,
    IncomingDetection,
    TrackManager,
)
from sensors.schemas.detection import ClassificationHint, ClassificationLabel, Provenance

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
SITE = GeodeticPoint(53.35, -6.26, 0.0)
R = isotropic_covariance(10.0)

SMALL = ClassificationLabel.SMALL_UAS
BIRD = ClassificationLabel.BIRD


@pytest.fixture
def clock() -> SimClock:
    return SimClock(START)


@pytest.fixture
def events(clock: SimClock) -> SqliteEventStore:
    store = SqliteEventStore(clock)
    yield store
    store.close()


@pytest.fixture
def manager(clock: SimClock, events: SqliteEventStore) -> TrackManager:
    return TrackManager(clock, events, LocalFrame(SITE))


@pytest.fixture
def service(events: SqliteEventStore, manager: TrackManager) -> ClassificationService:
    return ClassificationService(build_default_classifier(), events, sink=manager)


def changes_in(events: SqliteEventStore) -> list:
    """Every classification change in the log, oldest first."""
    return [
        e for e in events.read_from(0) if e.event_type == EventType.CLASSIFICATION_CHANGED
    ]


def hint(label: ClassificationLabel = SMALL, confidence: float = 0.85) -> ClassificationHint:
    return ClassificationHint(
        label=label, confidence=confidence, provenance=Provenance.MEASURED
    )


def run_cycle(
    manager: TrackManager,
    service: ClassificationService,
    clock: SimClock,
    index: int,
    *,
    label: ClassificationLabel | None = SMALL,
    dt: float = 1.0,
    speed: float = 14.0,
    altitude: float = 90.0,
):
    """One full tracking-then-classification cycle for a single object heading east.

    Mirrors `Pipeline.step`: detections in, tracker, then classification over what the
    tracker produced. Anything asserted after this call is what the rest of the system would
    actually see.
    """
    detection_id = f"det-{index}"
    tracks = manager.process(
        [
            IncomingDetection(
                detection_id=detection_id,
                sensor_id="SIM-RADAR-01",
                timestamp=clock.now(),
                position_enu=np.array([index * speed * dt, 0.0, altitude]),
                covariance=R,
            )
        ]
    )

    hints = {detection_id: hint(label)} if label is not None else {}
    classified = service.classify_cycle(
        tracks,
        CycleEvidence.from_assignments(manager.last_assignments, hints, {detection_id: 1.0}),
    )
    clock.advance(dt)
    return classified


# --------------------------------------------------------------------------------------
# Write-back — the defect this module exists to prevent recurring
# --------------------------------------------------------------------------------------


def test_classification_survives_the_next_tracking_cycle(
    manager: TrackManager, service: ClassificationService, clock: SimClock
) -> None:
    """Regression. A classified track must still be classified after tracking runs again.

    Asserted through `active_tracks()` — the tracker's own published view, which is what the
    projection and the API ultimately serve — rather than through the value `classify_cycle`
    returned. The returned copy was correct even while the system was broken.
    """
    for index in range(4):
        run_cycle(manager, service, clock, index)

    # Deliberately a *bare* tracking cycle: no classification afterwards, exactly the
    # situation in which the old code silently reverted the track.
    manager.process(
        [
            IncomingDetection(
                detection_id="det-99",
                sensor_id="SIM-RADAR-01",
                timestamp=clock.now(),
                position_enu=np.array([4 * 14.0, 0.0, 90.0]),
                covariance=R,
            )
        ]
    )

    track = manager.active_tracks()[0]
    assert track.classification.assessed
    assert track.classification.label == SMALL


def test_track_snapshot_on_a_tracking_event_carries_the_classification(
    manager: TrackManager, service: ClassificationService, clock: SimClock, events: SqliteEventStore
) -> None:
    """The log must be replayable into the same state the live system holds.

    If a `TRACK_UPDATED` snapshot omits the classification, a rebuild from the log produces a
    different picture than the running system — and the log stops being the authority the
    whole design rests on (ADR 0006).
    """
    for index in range(4):
        run_cycle(manager, service, clock, index)

    updates = [e for e in events.read_from(0) if e.event_type == EventType.TRACK_UPDATED]
    assert updates, "expected at least one tracking update to inspect"

    snapshot = updates[-1].payload[TRACK_SNAPSHOT_KEY]
    assert snapshot["classification"]["label"] == str(SMALL)
    assert snapshot["classification"]["distribution"]


def test_classification_for_a_retired_track_is_dropped(
    manager: TrackManager, service: ClassificationService, clock: SimClock
) -> None:
    """A track that no longer exists cannot be classified, and that is not an error."""
    result = service.classify_cycle(
        [],
        CycleEvidence(hints={}, quality={}),
    )
    assert result == []


# --------------------------------------------------------------------------------------
# Event emission — the log records changes, not heartbeats
# --------------------------------------------------------------------------------------


def test_first_real_assessment_is_recorded(
    manager: TrackManager, service: ClassificationService, clock: SimClock, events: SqliteEventStore
) -> None:
    """A NEW track is not displayable, so the first cycle classifies nothing — by design. The
    assessment appears on the cycle the track becomes visible."""
    run_cycle(manager, service, clock, 0)
    assert changes_in(events) == []

    run_cycle(manager, service, clock, 1)

    changes = changes_in(events)
    assert len(changes) == 1
    assert changes[0].payload["label"] == str(SMALL)
    assert changes[0].payload["evidence"]


def test_a_track_no_source_can_speak_to_emits_nothing(
    manager: TrackManager, service: ClassificationService, clock: SimClock, events: SqliteEventStore
) -> None:
    """Every source abstaining is a result, but not a change worth recording.

    A log that records non-events is one nobody reads. Here no sensor classified the track and
    it is moving far outside every configured envelope — 400 m/s at 9 km is an airliner, which
    is not a category this system models — so the kinematic source abstains too.
    """
    for index in range(6):
        run_cycle(manager, service, clock, index, label=None, speed=400.0, altitude=9000.0)

    assert changes_in(events) == []


def test_a_settled_assessment_stops_re_emitting(
    manager: TrackManager, service: ClassificationService, clock: SimClock, events: SqliteEventStore
) -> None:
    """Repeating the same evidence must not produce one event per cycle forever.

    Early cycles legitimately emit as the answer sharpens; what must not happen is a steady
    stream once it has settled.
    """
    for index in range(20):
        run_cycle(manager, service, clock, index)
    after_settling = len(changes_in(events))

    for index in range(20, 30):
        run_cycle(manager, service, clock, index)

    assert len(changes_in(events)) == after_settling, (
        "assessment kept re-emitting on unchanging evidence after it had settled"
    )


def test_a_changed_label_is_always_recorded(
    manager: TrackManager, service: ClassificationService, clock: SimClock, events: SqliteEventStore
) -> None:
    """A label flip is significant no matter how small the probability shift.

    This is the change an operator most needs to see, and thresholding it on probability
    movement would be exactly the wrong place to save an event.
    """
    for index in range(6):
        run_cycle(manager, service, clock, index, label=SMALL)

    before = len(changes_in(events))

    # Long enough to outweigh the accumulated small-UAS reports: the memory is deliberately
    # slow to change its mind, which is the point of it.
    for index in range(6, 40):
        run_cycle(manager, service, clock, index, label=BIRD)

    changes = changes_in(events)
    assert len(changes) > before
    assert changes[-1].payload["label"] == str(BIRD)


# --------------------------------------------------------------------------------------
# Evidence memory — a track is one object across cycles, not a fresh question each cycle
# --------------------------------------------------------------------------------------


def test_evidence_accumulates_across_cycles(
    manager: TrackManager, service: ClassificationService, clock: SimClock
) -> None:
    """Consistent reporting over many cycles must be more convincing than one report.

    Classifying each cycle in isolation made a well-observed track no more confidently
    labelled than a brand new one, and the answer flapped between labels on evidence that
    never actually changed.
    """
    run_cycle(manager, service, clock, 0)
    early = run_cycle(manager, service, clock, 1)[0]

    for index in range(2, 20):
        late = run_cycle(manager, service, clock, index)[0]

    assert late.classification.confidence > early.classification.confidence
    assert late.classification.label == SMALL


def test_a_coasting_gap_does_not_erase_what_was_learned(
    manager: TrackManager, service: ClassificationService, clock: SimClock
) -> None:
    """A sensor dropout is not evidence the earlier reports were wrong.

    Before the memory existed, a track that coasted for one cycle reverted to `unknown` —
    the display told the operator the system had forgotten what it had been confidently
    saying for the previous thirty seconds.
    """
    for index in range(10):
        run_cycle(manager, service, clock, index)

    clock.advance(1.0)
    tracks = manager.process([])
    coasting = service.classify_cycle(tracks, CycleEvidence(hints={}, quality={}))[0]

    assert coasting.tracking.is_coasting
    assert coasting.classification.label == SMALL
    assert coasting.classification.assessed


def test_the_memory_is_bounded(
    manager: TrackManager, service: ClassificationService, clock: SimClock
) -> None:
    """A long-lived track must not accumulate hints without limit."""
    for index in range(_HINT_MEMORY + 25):
        run_cycle(manager, service, clock, index)

    assert len(service._hints["TRK-00001"]) == _HINT_MEMORY  # noqa: SLF001


def test_forgetting_a_track_lets_a_reused_id_be_assessed_afresh(
    manager: TrackManager, service: ClassificationService, clock: SimClock
) -> None:
    """A remembered result must not suppress the first assessment of a later track.

    Track ids are not reused within a run, but the service's memory is keyed on them, and an
    unbounded map of every track ever seen is a slow leak as well as a correctness risk.
    """
    run_cycle(manager, service, clock, 0)
    run_cycle(manager, service, clock, 1)
    assert service._last  # noqa: SLF001 — asserting on the memory this test is about

    service.classify_cycle([], CycleEvidence(hints={}, quality={}))
    assert not service._last  # noqa: SLF001
