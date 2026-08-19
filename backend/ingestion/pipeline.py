"""Ingestion: the boundary every detection crosses on its way into the system.

Four things happen here, in order, and nothing downstream repeats any of them:

1. **Deduplicate.** The same `detection_id` arriving twice is recorded once. A repeat that
   reached the filter would double-weight one observation and make the estimate falsely
   confident (FM-013).
2. **Validate.** Bounds and timestamp sanity beyond what the schema already enforces. A
   rejection is recorded as an event with its reason, never dropped silently — a sensor
   producing bad data is itself a fact worth being able to reconstruct (FM-011).
3. **Record.** A `DETECTION_RECEIVED` event carrying the whole detection, appended *before*
   anything acts on it. This is what makes the log the source of truth rather than a summary
   of conclusions (ADR 0006), and it is what `GET /detections` reads.
4. **Reduce.** Convert to the tracker's input: position, uncertainty, time, sensor. Speed,
   heading and classification are dropped here and never cross into tracking, so the tracker
   is structurally incapable of depending on data a real sensor may not supply (ADR 0002).

Step 4 drops the classification hint from the *tracker's* input, but ingestion still returns
it separately, keyed by detection, for the classifier. That is the distinction ADR 0002 draws:
the hint must not influence where a track is, and is legitimate evidence for what it is.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np

from backend.api.schemas import DETECTION_SNAPSHOT_KEY
from backend.common.clock import Clock
from backend.events.store import EventStore
from backend.events.types import EventType
from backend.tracking.filters.kalman import isotropic_covariance
from backend.tracking.track_manager.manager import IncomingDetection
from backend.zones.geometry import SiteGeometry
from sensors.schemas.detection import ClassificationHint, Detection

SOURCE = "ingestion"

# How many recently-seen detection ids are remembered for duplicate suppression. Bounded
# because a long-running system would otherwise accumulate one entry per detection forever;
# a duplicate arriving later than this is a different problem from a retransmission.
DEFAULT_DEDUPE_WINDOW = 10_000


@dataclass(slots=True)
class IngestionResult:
    """One cycle's worth of ingested detections.

    `for_tracking` and `hints` are separate deliberately. Handing the tracker a structure that
    still contained classification would make ADR 0002's guarantee a matter of the tracker
    choosing not to look, rather than of it having nothing to look at.

    Mutable, and honestly so: it accumulates over one cycle's detections. A frozen wrapper
    around mutable lists would advertise a guarantee it could not keep.
    """

    for_tracking: list[IncomingDetection] = field(default_factory=list)
    hints: dict[str, ClassificationHint] = field(default_factory=dict)
    """detection_id -> the classification the sensor reported, where it reported one."""

    quality: dict[str, float] = field(default_factory=dict)
    """detection_id -> the sensor's confidence that this detection is a real object."""

    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0


class IngestionPipeline:
    """Validates, records and normalises detections. The single entry point into the system."""

    def __init__(
        self,
        clock: Clock,
        events: EventStore,
        geometry: SiteGeometry,
        max_clock_skew_s: float = 30.0,
        dedupe_window: int = DEFAULT_DEDUPE_WINDOW,
    ) -> None:
        self._clock = clock
        self._events = events
        self._geometry = geometry
        self._max_skew = timedelta(seconds=max_clock_skew_s)
        self._dedupe_window = dedupe_window
        # Ordered so the oldest id can be evicted once the window is full. Values are unused;
        # only membership and insertion order matter.
        self._seen: OrderedDict[str, None] = OrderedDict()

    def ingest(self, detections: list[Detection]) -> IngestionResult:
        result = IngestionResult()

        for detection in detections:
            if detection.detection_id in self._seen:
                result.duplicates += 1
                self._reject(detection, "duplicate_detection_id")
                continue

            problem = self._validate(detection)
            if problem is not None:
                result.rejected += 1
                self._reject(detection, problem)
                continue

            self._remember(detection.detection_id)

            # Recorded before it is reduced or acted on. Everything downstream reads a log
            # that already contains this (ADR 0006).
            self._events.append(
                EventType.DETECTION_RECEIVED,
                SOURCE,
                payload={DETECTION_SNAPSHOT_KEY: detection.model_dump(mode="json")},
            )

            result.for_tracking.append(self._reduce(detection))
            result.quality[detection.detection_id] = detection.quality.confidence
            if detection.classification is not None:
                result.hints[detection.detection_id] = detection.classification

            result.accepted += 1

        return result

    def _validate(self, detection: Detection) -> str | None:
        """Checks the schema cannot make. Returns a reason code, or None if acceptable."""
        now = self._clock.now()
        skew = detection.timestamp - now

        if skew > self._max_skew:
            # A detection from the future cannot be a late arrival, so this is a clock
            # problem rather than a network one (FM-012).
            return "timestamp_in_future"
        if -skew > self._max_skew:
            return "timestamp_too_old"

        if not _has_usable_uncertainty(detection):
            # A sensor claiming perfect (or degenerate) positional knowledge is not reporting
            # a precise measurement, it is reporting a broken one. The schema permits it —
            # `position_uncertainty_m` is `ge=0.0` and the covariance validator only rejects
            # negative variances — and the filter cannot survive it: the innovation covariance
            # becomes singular and both the association gate and the update raise
            # `LinAlgError`, taking down the whole tracking cycle rather than one detection.
            #
            # Rejected here, where rejections are counted and recorded, so one malformed
            # sensor costs its own detections and nothing else (FM-011).
            return "unusable_position_uncertainty"

        return None

    def _reject(self, detection: Detection, reason: str) -> None:
        self._events.append(
            EventType.DETECTION_REJECTED,
            SOURCE,
            payload={
                "detection_id": detection.detection_id,
                "sensor_id": detection.sensor.sensor_id,
                "reason": reason,
                # The timestamp is what most rejections are about, so it is worth having in
                # the record without needing to find the original detection.
                "timestamp": detection.timestamp.isoformat(),
            },
        )

    def _reduce(self, detection: Detection) -> IncomingDetection:
        """Canonical Detection to the tracker's input, in the site's local ENU frame."""
        enu = self._geometry.to_enu(
            detection.position.latitude,
            detection.position.longitude,
            detection.position.altitude_m,
        )

        return IncomingDetection(
            detection_id=detection.detection_id,
            sensor_id=detection.sensor.sensor_id,
            timestamp=detection.timestamp,
            position_enu=np.array([enu.east_m, enu.north_m, enu.up_m]),
            covariance=_stated_covariance(detection),
        )

    def _remember(self, detection_id: str) -> None:
        self._seen[detection_id] = None
        while len(self._seen) > self._dedupe_window:
            self._seen.popitem(last=False)


def _stated_covariance(detection: Detection) -> np.ndarray:
    """The measurement covariance this detection asserts, full matrix or isotropic fallback.

    One implementation, used by both validation and reduction. It used to be written inline in
    `_reduce`, which meant the scalar path never went through `isotropic_covariance` and its
    positivity check never ran — a second copy of a conversion that already existed, differing
    from the original in exactly the check that mattered.

    Raises on a scalar it cannot represent. Callers reach it only through
    `_has_usable_uncertainty`, which turns that into a rejection instead of a crash.
    """
    stated = detection.measurement.position_covariance_enu
    if stated is not None:
        return np.array(stated, dtype=float)
    return isotropic_covariance(detection.measurement.position_uncertainty_m)


def _has_usable_uncertainty(detection: Detection) -> bool:
    """Whether this detection's stated uncertainty can actually be used as a covariance.

    Positive-definiteness by Cholesky rather than a check on the diagonal: a matrix can have a
    positive variance on every axis and still be singular if two of them are perfectly
    correlated, and it is singularity — not smallness — that makes the filter raise.

    Total by construction: a value `isotropic_covariance` refuses to build is unusable by
    definition, so the exception is classified here rather than propagating out of validation.
    """
    try:
        covariance = _stated_covariance(detection)
    except ValueError:
        return False

    try:
        np.linalg.cholesky(covariance)
    except np.linalg.LinAlgError:
        return False
    return True
