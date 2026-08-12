"""System health: whether the system is working, as distinct from whether it is running.

Only the pipeline-liveness half exists so far (`cycles`), which is what makes an event-store
outage visible rather than silent (FM-032). The watchdog proper — sensor-silence detection and
the degraded-mode controller (FM-010, spec §16) — is not built.
"""

from backend.system_health.cycles import CycleHealth

__all__ = ["CycleHealth"]
