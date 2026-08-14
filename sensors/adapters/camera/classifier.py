"""What a cropped region is, according to a small vision model.

Takes pixels, returns a label from the canonical closed set and a confidence. It knows nothing
about streams, adapters, bearings or detections, which is what makes it swappable: replacing
the model, or replacing the whole library, touches this file and no other.

## Availability

The inference library is an optional dependency. It is heavy, it is not needed to run the
radar path or the tests, and a machine without it must still be able to import and run the
camera adapter — so the import happens lazily, once, and a failure is reported through
`is_available` and `unavailable_reason` rather than as an exception at import time.

An unavailable classifier does not disable the camera. `classify()` returns `UNKNOWN` at zero
confidence, and the adapter goes on emitting detections for regions that moved. That is the
honest degradation: the camera really did see something move, and it really cannot say what it
was. Dropping the detection instead would report an empty sky (FM-010).

## Label mapping

The model is trained on a general-purpose object set whose vocabulary is not ours. The mapping
below is deliberately narrow and explicit: anything not listed becomes `UNKNOWN` rather than
being guessed at. A model trained on everyday photographs has no drone class and cannot
acquire one by our renaming its nearest neighbour — an aircraft-labelled crop is evidence of a
flying object, not evidence of a small UAS, and asserting otherwise here would put a fiction
into the classification module's evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

import numpy as np

from sensors.schemas.detection import ClassificationLabel

DEFAULT_MODEL_NAME = "yolov8n.pt"

INSTALL_HINT = (
    "the camera classifier needs the optional inference dependencies. "
    "Install them with:  pip install -e '.[camera]'"
)

_LABEL_MAP: dict[str, ClassificationLabel] = {
    "bird": ClassificationLabel.BIRD,
    "airplane": ClassificationLabel.OTHER,
    "kite": ClassificationLabel.OTHER,
    "car": ClassificationLabel.VEHICLE,
    "truck": ClassificationLabel.VEHICLE,
    "bus": ClassificationLabel.VEHICLE,
    "motorcycle": ClassificationLabel.VEHICLE,
}
"""General-purpose model vocabulary to the canonical closed set.

No entry maps to any UAS label. That is not an omission: a model trained on everyday
photographs has no drone class, and inventing one by aliasing `airplane` would manufacture
evidence the model never produced.
"""


@dataclass(frozen=True, slots=True)
class Classification:
    """One verdict about one crop."""

    label: ClassificationLabel
    confidence: float

    @property
    def is_confident(self) -> bool:
        return self.confidence > 0.0 and self.label is not ClassificationLabel.UNKNOWN


UNKNOWN = Classification(ClassificationLabel.UNKNOWN, 0.0)
"""What a classifier that could not answer returns. Zero confidence, not a low one — the model
did not weakly believe this, it did not run."""


class Classifier:
    """A lazily loaded vision model behind a two-method surface.

    The model is loaded once, on first use, and shared for the process lifetime: loading costs
    seconds and hundreds of megabytes, and a per-call load would make the adapter's poll period
    a function of model size. Loading on first use rather than at import keeps the cost off any
    process that imports this module without classifying — the radar path, and most tests.

    Construction never fails and never raises. A missing library, a missing weights file and a
    corrupt download all end in the same place: `is_available` False, a reason in words, and
    `classify()` returning `UNKNOWN`.
    """

    __slots__ = ("_model_name", "_confidence_threshold", "_inference_size_px",
                 "_model", "_load_attempted", "_unavailable_reason", "_lock")

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_MODEL_NAME,
        confidence_threshold: float = 0.35,
        inference_size_px: int = 320,
    ) -> None:
        self._model_name = model_name
        self._confidence_threshold = confidence_threshold
        self._inference_size_px = inference_size_px
        self._model: object | None = None
        self._load_attempted = False
        self._unavailable_reason: str | None = None
        self._lock = Lock()

    @property
    def is_available(self) -> bool:
        """Whether a model is loaded and able to answer. Triggers the load on first call."""
        self._ensure_loaded()
        return self._model is not None

    @property
    def unavailable_reason(self) -> str | None:
        """Why the classifier cannot answer, in words, or None when it can.

        Surfaced verbatim in the camera adapter's health record, so that a machine missing the
        dependency says so in the operator interface rather than quietly classifying nothing.
        """
        self._ensure_loaded()
        return self._unavailable_reason

    def classify(self, crop: np.ndarray) -> Classification:
        """The most confident recognised object in a crop, or `UNKNOWN`.

        Never raises. An inference failure on one frame is not a reason to take the sensor
        down, so it is absorbed into `UNKNOWN` and reported through `unavailable_reason`.
        """
        if crop.size == 0:
            return UNKNOWN
        self._ensure_loaded()
        if self._model is None:
            return UNKNOWN

        try:
            results = self._model.predict(  # type: ignore[attr-defined]
                crop,
                imgsz=self._inference_size_px,
                conf=self._confidence_threshold,
                verbose=False,
            )
        except Exception as exc:  # noqa: BLE001 - any inference failure degrades identically
            self._unavailable_reason = f"inference failed: {exc}"
            return UNKNOWN

        return self._best(results)

    def _best(self, results: object) -> Classification:
        """Highest-confidence recognised box across the model's output.

        Boxes whose class is not in `_LABEL_MAP` are skipped rather than reported as UNKNOWN
        with the model's confidence: the model was confident about a label we do not carry,
        which is not the same as being confident that the object is unidentifiable.
        """
        best = UNKNOWN
        for result in results:  # type: ignore[union-attr]
            names = getattr(result, "names", {}) or {}
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            for box in boxes:
                confidence = float(box.conf[0])
                if confidence < self._confidence_threshold or confidence <= best.confidence:
                    continue
                raw_name = str(names.get(int(box.cls[0]), "")).lower()
                label = _LABEL_MAP.get(raw_name)
                if label is None:
                    continue
                best = Classification(label, confidence)
        return best

    def _ensure_loaded(self) -> None:
        """Load the model once. Repeated failures do not retry — the reason will not change."""
        if self._load_attempted:
            return
        with self._lock:
            if self._load_attempted:
                return
            self._load_attempted = True
            try:
                from ultralytics import YOLO
            except ImportError as exc:
                self._unavailable_reason = f"{INSTALL_HINT} ({exc})"
                return
            try:
                self._model = YOLO(self._model_name)
            except Exception as exc:  # noqa: BLE001 - missing weights, bad file, no disk
                self._unavailable_reason = (
                    f"could not load model {self._model_name!r}: {exc}"
                )
