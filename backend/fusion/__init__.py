"""Multi-sensor reconciliation: several sensors' observations of one object, made one.

See `README.md` for the design and its limits, ADR 0018 for why it works on detections rather
than on tracks, and FM-023 for the failure it closes.
"""

from backend.fusion.models import FusedDetection
from backend.fusion.policy import FusionPolicy, load_fusion_policy, policy_path
from backend.fusion.reconcile import (
    MULTI_SENSOR_MARKER,
    DetectionReconciler,
    FusionResult,
    to_incoming,
)

__all__ = [
    "MULTI_SENSOR_MARKER",
    "DetectionReconciler",
    "FusedDetection",
    "FusionPolicy",
    "FusionResult",
    "load_fusion_policy",
    "policy_path",
    "to_incoming",
]
