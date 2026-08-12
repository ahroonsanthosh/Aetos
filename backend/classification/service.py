"""Running classification over a tracking cycle's output.

Sits between the tracker and the event log: takes the tracks a cycle produced, assembles the
evidence for each, classifies, and records a `CLASSIFICATION_CHANGED` event carrying the
updated track wherever the answer actually moved.

Why a separate service rather than a method on the tracker: tracking answers *where*,
classification answers *what*, and the boundary between them is the thing that lets either be
replaced without touching the other (docs/architecture.md). The tracker never learns that
classification exists.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol

from backend.classification.classifier import Classifier
from backend.classification.evidence import TrackEvidence
from backend.classification.models import ClassificationResult
from backend.events.store import EventStore
from backend.events.types import EventType
from backend.tracking.models import Track, TrackClassification
from backend.tracking.track_manager.manager import TRACK_SNAPSHOT_KEY
from sensors.schemas.detection import ClassificationHint

SOURCE = "classification"

# How much the top label's probability must move before a change is recorded. Without a
# threshold, floating-point drift on an otherwise-identical assessment would emit an event
# every cycle, and a log that records non-events is one nobody can read.
_SIGNIFICANT_SHIFT = 0.05

_HINT_MEMORY = 40
"""How many of a track's most recent sensor hints classification reasons over.

What a track *is* does not change between cycles; what the sensors happened to report in the
last half-second does. Classifying from one cycle's hints in isolation made the answer flap —
`bird`, then `other`, then `unknown` — on a track that was one object throughout, and made a
brief coasting gap erase everything already learned about it. Neither is a defensible thing to
show an operator.

Bounded rather than unbounded so the memory still forgets. If association ever hands a track
to the wrong object, an unbounded tally would keep the old identity alive indefinitely; forty
cycles is long enough to be stable and short enough to be correctable. It is also a fixed
per-track ceiling, which matters on a long run.
"""


@dataclass(frozen=True, slots=True)
class CycleEvidence:
    """The per-track evidence one cycle produced, keyed by track.

    Assembled by the caller from ingestion's hints and the tracker's association, because
    only the caller sees both. Classification deliberately does not reach back into either.
    """

    hints: dict[str, tuple[ClassificationHint, ...]]
    quality: dict[str, tuple[float, ...]]

    @classmethod
    def from_assignments(
        cls,
        assignments: dict[str, str],
        hints_by_detection: dict[str, ClassificationHint],
        quality_by_detection: dict[str, float],
    ) -> CycleEvidence:
        """Route per-detection hints to the tracks the association gave them to."""
        return cls._route(
            {track_id: (detection_id,) for track_id, detection_id in assignments.items()},
            hints_by_detection,
            quality_by_detection,
        )

    @classmethod
    def from_fused_assignments(
        cls,
        assignments: dict[str, str],
        contributions: dict[str, tuple[str, ...]],
        hints_by_detection: dict[str, ClassificationHint],
        quality_by_detection: dict[str, float],
    ) -> CycleEvidence:
        """Route hints when the tracker was fed fused measurements (FM-024).

        A fused detection stands for several real ones, so the track it was assigned to is
        entitled to *every* contributing sensor's opinion — not just whichever detection's
        identifier survived the fusion.

        Without this, being better observed would make a track *less* confidently classified:
        three sensors would contribute one hint between them, and the evidence pool would grow
        no faster than on a single-sensor site while the operator reasonably expected the
        opposite. `contributions` maps a fused identifier back to its parts; an identifier
        absent from it was never fused and stands for itself.
        """
        return cls._route(
            {
                track_id: contributions.get(detection_id, (detection_id,))
                for track_id, detection_id in assignments.items()
            },
            hints_by_detection,
            quality_by_detection,
        )

    @classmethod
    def _route(
        cls,
        detections_by_track: dict[str, tuple[str, ...]],
        hints_by_detection: dict[str, ClassificationHint],
        quality_by_detection: dict[str, float],
    ) -> CycleEvidence:
        """The one routing implementation, shared by both entry points above.

        Detections carrying no hint are skipped rather than contributing a neutral one: a sensor
        that reported no opinion has not reported an uncertain opinion, and padding the pool
        with the difference would dilute the sensors that did.
        """
        hints: dict[str, tuple[ClassificationHint, ...]] = {}
        quality: dict[str, tuple[float, ...]] = {}

        for track_id, detection_ids in detections_by_track.items():
            for detection_id in detection_ids:
                hint = hints_by_detection.get(detection_id)
                if hint is None:
                    continue
                hints[track_id] = (*hints.get(track_id, ()), hint)
                quality[track_id] = (
                    *quality.get(track_id, ()),
                    quality_by_detection.get(detection_id, 1.0),
                )

        return cls(hints=hints, quality=quality)


class ClassificationSink(Protocol):
    """Where a finished classification is written back to.

    Narrowed to the one method rather than typed as `TrackManager`, so this module depends on
    a capability instead of on the tracker — the direction of that dependency is the thing
    keeping tracking replaceable without touching classification.
    """

    def apply_classification(
        self, track_id: str, classification: TrackClassification
    ) -> Track | None:
        """Store the classification, returning the updated track, or None if it is gone."""
        ...


class ClassificationService:
    """Classifies a cycle's tracks and records what changed."""

    def __init__(
        self, classifier: Classifier, events: EventStore, sink: ClassificationSink
    ) -> None:
        self._classifier = classifier
        self._events = events
        self._sink = sink
        # Last recorded result per track, so an unchanged assessment does not re-emit.
        self._last: dict[str, ClassificationResult] = {}
        # Recent sensor hints per track, oldest first. See `_HINT_MEMORY`.
        self._hints: dict[str, deque[tuple[ClassificationHint, float]]] = {}

    def classify_cycle(self, tracks: list[Track], evidence: CycleEvidence) -> list[Track]:
        """Classify each track, returning the tracks with classification applied.

        Returns every track, changed or not, so the caller has one consistent picture rather
        than having to merge a partial update back over its own copy.

        The result is written back through the sink rather than only into the copy returned
        here. Without that, the next cycle's `TRACK_UPDATED` — emitted by the component that
        owns track state, from a snapshot with no classification in it — reverts the track to
        `unknown`, and the API serves `unknown` for a track this module has confidently
        labelled. That was a real defect, caught only by reading what the endpoint actually
        returned rather than by counting the events written.
        """
        classified: list[Track] = []

        for track in tracks:
            remembered = self._remember(track.track_id, evidence)

            result = self._classifier.classify(
                TrackEvidence(
                    track_id=track.track_id,
                    speed_mps=track.kinematics.speed_mps,
                    altitude_m=track.position.altitude_m,
                    vertical_rate_mps=track.kinematics.vertical_rate_mps,
                    detection_count=track.tracking.detection_count,
                    coasting=track.tracking.is_coasting,
                    hints=tuple(hint for hint, _ in remembered),
                    hint_quality=tuple(quality for _, quality in remembered),
                )
            )

            # The owner's updated view is preferred over a local copy: if the two ever
            # disagree, the owner is right by definition, and returning its version keeps
            # this module from becoming a second source of truth for track state.
            stored = self._sink.apply_classification(
                track.track_id, _to_track_classification(result)
            )
            if stored is None:
                # Retired between tracking and classification. Nothing to record about a
                # track that no longer exists.
                continue

            classified.append(stored)

            if self._is_significant(track.track_id, result):
                self._record(stored, result)
                self._last[track.track_id] = result

        self._forget_absent(tracks)
        return classified

    def _remember(
        self, track_id: str, evidence: CycleEvidence
    ) -> tuple[tuple[ClassificationHint, float], ...]:
        """Add this cycle's hints to the track's memory and return everything remembered.

        A cycle contributing nothing is normal — most sensors do not classify, and a coasting
        track receives no detection at all. It leaves the memory untouched rather than
        clearing it, which is the whole point: a gap in reporting is not evidence that the
        earlier reports were wrong.
        """
        hints = evidence.hints.get(track_id, ())
        quality = evidence.quality.get(track_id, ())

        if hints:
            memory = self._hints.setdefault(track_id, deque(maxlen=_HINT_MEMORY))
            for index, hint in enumerate(hints):
                memory.append((hint, quality[index] if index < len(quality) else 1.0))

        return tuple(self._hints.get(track_id, ()))

    def _is_significant(self, track_id: str, result: ClassificationResult) -> bool:
        """Whether this differs enough from the last recorded answer to be worth an event."""
        previous = self._last.get(track_id)
        if previous is None:
            # Never assessed. Worth recording only once there is something to say — otherwise
            # every track would emit a "still unknown" event on its first cycle.
            return result.assessed

        if previous.label != result.label:
            return True

        previous_top = previous.distribution.get(previous.label, 0.0)
        current_top = result.distribution.get(result.label, 0.0)
        return abs(current_top - previous_top) >= _SIGNIFICANT_SHIFT

    def _record(self, track: Track, result: ClassificationResult) -> None:
        self._events.append(
            EventType.CLASSIFICATION_CHANGED,
            SOURCE,
            payload={
                "label": str(result.label),
                "confidence": result.confidence,
                "distribution": {str(k): v for k, v in result.distribution.items()},
                "evidence": [
                    {
                        "source": contribution.source,
                        "weight": round(contribution.weight, 4),
                        "detail": contribution.detail,
                    }
                    for contribution in result.evidence
                ],
                # The full track, as every projected track event carries, so classification
                # is reconstructible from the log like any other state (ADR 0006).
                TRACK_SNAPSHOT_KEY: track.model_dump(mode="json"),
            },
            track_id=track.track_id,
        )

    def _forget_absent(self, tracks: list[Track]) -> None:
        """Drop everything remembered about tracks that are no longer present.

        Without this the service would accumulate one entry per track ever seen — a slow leak
        on a long run — and a track id reused after a restart would be assessed against a
        previous object's evidence.
        """
        present = {track.track_id for track in tracks}
        for track_id in [t for t in self._last if t not in present]:
            del self._last[track_id]
        for track_id in [t for t in self._hints if t not in present]:
            del self._hints[track_id]


def _to_track_classification(result: ClassificationResult) -> TrackClassification:
    return TrackClassification(
        label=result.label,
        confidence=result.confidence,
        distribution=dict(result.distribution),
        reasons=[c.detail for c in result.evidence if c.weight > 0.0],
    )
