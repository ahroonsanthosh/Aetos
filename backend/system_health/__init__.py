"""System health: the watchdog and the degraded-mode declaration.

See `watchdog.py` for what this module is allowed to conclude and why the limits are drawn
where they are.
"""

from backend.system_health.models import (
    HealthAssessment,
    Posture,
    SensorAssessment,
    SensorVerdict,
)
from backend.system_health.policy import (
    SystemHealthPolicy,
    load_system_health_policy,
    policy_path,
)
from backend.system_health.watchdog import Watchdog

__all__ = [
    "HealthAssessment",
    "Posture",
    "SensorAssessment",
    "SensorVerdict",
    "SystemHealthPolicy",
    "Watchdog",
    "load_system_health_policy",
    "policy_path",
]
