"""Watchdog and degraded mode: noticing that the picture has thinned, and saying so.

See `watchdog.py` for the reasoning, in particular how the "broken sensor versus empty sky"
ambiguity at the centre of FM-010 is resolved rather than guessed at.
"""

from __future__ import annotations

from backend.system_health.models import (
    SensorCondition,
    SensorObservation,
    SystemHealth,
)
from backend.system_health.policy import HealthPolicy, load_health_policy
from backend.system_health.watchdog import SensorWatchdog

__all__ = [
    "HealthPolicy",
    "SensorCondition",
    "SensorObservation",
    "SensorWatchdog",
    "SystemHealth",
    "load_health_policy",
]
