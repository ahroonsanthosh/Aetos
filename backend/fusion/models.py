"""What fusion produces, and what it remembers about how it got there."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np


@dataclass(frozen=True, slots=True)
class FusedDetection:
    """One object, as reconciled from every sensor that saw it this cycle.

    Deliberately shaped so that `IngestionPipeline`'s output and this are interchangeable to the
    tracker: same position, same covariance, same timestamp, same identifier field. The tracker
    must not be able to tell whether fusion ran, for the same reason it must not be able to tell
    the simulator from a real sensor (ADR 0002) — a tracker that behaved differently on fused
    input would have to be tested twice, and the untested path is the one that ships.

    What is added is *provenance*, which nothing upstream can reconstruct afterwards: which
    detections were combined, and which sensors they came from.
    """

    detection_id: str
    """The identifier the tracker sees.

    For a single-sensor observation this is the original detection's id, unchanged — fusing one
    detection must be indistinguishable from not fusing at all. For a genuine combination it is
    derived from the contributing ids, so the fused measurement is traceable back to its parts
    in the log rather than appearing from nowhere.
    """

    sensor_id: str
    """The reporting sensor for a single-sensor observation, or a marker naming the set.

    Retained because `IncomingDetection` has it and the tracker records it on the track's
    contributing-sensor set. `contributing_sensor_ids` is the honest field; this one exists so
    the shapes match.
    """

    timestamp: datetime
    position_enu: np.ndarray
    covariance: np.ndarray

    contributing_detection_ids: tuple[str, ...] = field(default_factory=tuple)
    """Every detection combined into this one, in stable order.

    This is what carries classification hints through fusion. Without it, an object seen by
    three sensors would arrive at the classifier with one sensor's opinion instead of three,
    which is FM-024 — and the resulting classification would be *less* confident for being
    better observed.
    """

    contributing_sensor_ids: tuple[str, ...] = field(default_factory=tuple)
    """The distinct sensors behind this measurement, in stable order."""

    @property
    def is_multi_sensor(self) -> bool:
        return len(self.contributing_sensor_ids) > 1
