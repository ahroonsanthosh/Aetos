"""Multi-sensor reconciliation: which tracks are observations of one object.

See `service.py` for the reasoning, and `README.md` for what this module must not do.
"""

from __future__ import annotations

from backend.fusion.models import FusedObject
from backend.fusion.policy import FusionPolicy, load_fusion_policy
from backend.fusion.service import FusionService

__all__ = ["FusedObject", "FusionPolicy", "FusionService", "load_fusion_policy"]
