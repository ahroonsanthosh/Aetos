"""The sensor watchdog: noticing that a sensor has stopped, and saying so.

## The problem this exists for

A sensor stops producing. Its tracks coast, go stale, and are lost, and throughout that the
interface keeps drawing confident-looking positions derived from data that no longer exists.
Nothing anywhere says the picture has thinned. That is FM-010, and it matters far more on a
site than on a laptop, because on a laptop somebody is watching the terminal.

## The ambiguity at the centre of it, and how it is resolved

**A sensor producing nothing may be broken, or may be looking at an empty sky.** One sensor,
watched alone, cannot distinguish those — and a watchdog that guessed would be wrong in one of
two expensive directions. Guess "broken" and it cries wolf through every quiet minute until an
operator learns to ignore the indicator, which is the failure ADR 0015 spends its whole length
avoiding for alerts. Guess "empty sky" and it stays silent through exactly the failure it was
built to catch.

So it does not guess. It uses the one piece of evidence a single sensor does not have: **the
other sensors.** Silence while neighbours are producing steadily is anomalous. Silence while
nothing anywhere is producing is a quiet sky, and is reported as OK with the reason saying so.

That rule is not a heuristic standing in for a better one — it is the actual discriminating
information available, and it degrades honestly: a single-sensor site has no neighbours, so it
can never call its sensor silent, and the reason text says exactly that rather than implying a
health check that is not happening.

## What it must not do

**It must not suppress alerts** (`docs/architecture.md`, module map). A degraded site is a
reason to tell an operator more, never less. Nothing here touches alerting, and a track's risk
is scored the same whether the watchdog is happy or not — a system that quietened down as its
sensing failed would be at its most reassuring when it was least able to see.
"""

from __future__ import annotations

from datetime import datetime

from backend.common.clock import Clock
from backend.events.store import EventStore
from backend.events.types import EventType
from backend.system_health.models import (
    SensorCondition,
    SensorObservation,
    SystemHealth,
)
from backend.system_health.policy import HealthPolicy
from sensors.adapters.interface import SensorHealth, SensorState

SOURCE = "system_health"

_STATE_TO_CONDITION = {
    SensorState.NOT_STARTED: SensorCondition.NOT_STARTED,
    SensorState.OK: SensorCondition.OK,
    SensorState.DEGRADED: SensorCondition.DEGRADED,
    SensorState.OFFLINE: SensorCondition.OFFLINE,
}


class SensorWatchdog:
    """Polls sensor health each cycle, decides what it means, and records what changed."""

    def __init__(
        self,
        policy: HealthPolicy,
        clock: Clock,
        events: EventStore,
        expected_intervals_s: dict[str, float] | None = None,
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._events = events
        self._expected = dict(expected_intervals_s or {})

        self._conditions: dict[str, SensorCondition] = {}
        """sensor_id -> last reported condition, so only changes are recorded."""

        self._observed_since: dict[str, datetime] = {}
        """sensor_id -> when this sensor was first seen running.

        Silence has to be measured from *something*. A sensor that has never produced a
        detection has no `last_detection_time`, and measuring from process start would call a
        sensor silent for a gap that happened before it was even polled."""

        self._health = SystemHealth()
        self._degraded = False

    @property
    def health(self) -> SystemHealth:
        """The most recent assessment. Read out of band by the API."""
        return self._health

    def set_expected_interval(self, sensor_id: str, interval_s: float) -> None:
        """Declare how often a sensor is expected to report.

        Supplied by whatever assembled the adapters, because that is what knows the registry.
        The watchdog deliberately does not read the sensor registry itself — it would then only
        work for sensors that have registry entries, which real hardware may not.
        """
        self._expected[sensor_id] = interval_s

    def poll(self, reports: list[SensorHealth]) -> SystemHealth:
        """Assess one cycle's sensor health. Returns the site's posture."""
        now = self._clock.now()

        # Pass one: is anything at all producing? This is the evidence that makes one sensor's
        # silence interpretable, so it has to be established before any sensor is judged.
        someone_producing = any(self._is_producing(report, now) for report in reports)

        observations = [
            self._assess(report, now, someone_producing=someone_producing) for report in reports
        ]
        self._record_condition_changes(observations)

        contributing = sum(1 for o in observations if o.condition.is_contributing)
        health = SystemHealth(
            assessed=True,
            degraded=contributing < self._policy.minimum_contributing_sensors,
            reason=self._posture_reason(contributing, len(observations)),
            sensors=observations,
            contributing_sensors=contributing,
            configured_sensors=len(observations),
        )

        self._record_degraded_transition(health)
        self._forget_absent({report.sensor_id for report in reports})
        self._health = health
        return health

    def _forget_absent(self, present: set[str]) -> None:
        """Drop remembered state for sensors no longer being reported.

        Both maps are keyed by sensor id and nothing removed from them. Real adapters have
        stable identifiers so this is bounded in practice, but a deployment that renames or
        rotates sensors — or a long-lived process across several site configurations — grew them
        without limit. Found by pentest: 300 cycles of changing identifiers left 307 entries
        behind, and nothing would ever have reclaimed them.
        """
        for sensor_id in [s for s in self._conditions if s not in present]:
            del self._conditions[sensor_id]
        for sensor_id in [s for s in self._observed_since if s not in present]:
            del self._observed_since[sensor_id]

    # -----------------------------------------------------------------------------------
    # Per-sensor assessment
    # -----------------------------------------------------------------------------------

    def _assess(
        self, report: SensorHealth, now: datetime, *, someone_producing: bool
    ) -> SensorObservation:
        expected = self._expected.get(report.sensor_id)
        threshold = self._threshold_for(report)

        if report.state in (SensorState.OK, SensorState.DEGRADED):
            self._observed_since.setdefault(report.sensor_id, now)
        else:
            # A stopped sensor's silence clock restarts when it comes back, rather than
            # counting the time it spent legitimately offline against it.
            self._observed_since.pop(report.sensor_id, None)

        silence = self._silence_seconds(report, now)
        condition = _STATE_TO_CONDITION[report.state]
        reason = report.reason

        if (
            condition.is_contributing
            and silence is not None
            and silence >= threshold
            and report.detections_emitted > 0
        ):
            if someone_producing:
                condition = SensorCondition.SILENT
                reason = (
                    f"No detections for {silence:.0f} s while other sensors are reporting, "
                    f"past this sensor's {threshold:.0f} s allowance."
                )
            else:
                # Nothing anywhere is producing, so this sensor's silence says nothing about
                # this sensor. Reported as the fact it is, without the accusation.
                reason = (
                    f"No detections for {silence:.0f} s, but no sensor is currently reporting — "
                    "consistent with an empty sky. Cannot distinguish that from a fault."
                )

        return SensorObservation(
            sensor_id=report.sensor_id,
            sensor_type=report.sensor_type,
            condition=condition,
            reason=reason,
            last_detection_time=report.last_detection_time,
            detections_emitted=report.detections_emitted,
            silent_for_s=round(silence, 3) if silence is not None else None,
            expected_interval_s=expected,
        )

    def _is_producing(self, report: SensorHealth, now: datetime) -> bool:
        """Whether this sensor is currently delivering, and so can vouch for the sky being busy.

        The evidence that makes another sensor's silence interpretable, so it is deliberately
        strict: the sensor must be up, must have produced something, and must have produced it
        recently by its own threshold.
        """
        if report.state not in (SensorState.OK, SensorState.DEGRADED):
            return False
        if report.detections_emitted <= 0:
            return False
        silence = self._silence_seconds(report, now)
        return silence is not None and silence < self._threshold_for(report)

    def _silence_seconds(self, report: SensorHealth, now: datetime) -> float | None:
        """How long this sensor has produced nothing, or None if that is not yet meaningful."""
        if report.last_detection_time is not None:
            return max(0.0, (now - report.last_detection_time).total_seconds())

        since = self._observed_since.get(report.sensor_id)
        if since is None:
            return None
        return max(0.0, (now - since).total_seconds())

    def _threshold_for(self, report: SensorHealth) -> float:
        return self._policy.silence_threshold_s(report.sensor_id, str(report.sensor_type))

    # -----------------------------------------------------------------------------------
    # Recording
    # -----------------------------------------------------------------------------------

    def _posture_reason(self, contributing: int, configured: int) -> str:
        if contributing >= self._policy.minimum_contributing_sensors:
            return (
                f"{contributing} of {configured} sensors contributing; "
                f"{self._policy.minimum_contributing_sensors} required."
            )
        if configured == 0:
            # Distinct from sensors that have failed, and worth wording differently: nothing was
            # ever attached, so there is nothing to have gone wrong. An operator reading
            # "0 of 0 sensors are contributing" would reasonably wonder what broke.
            return (
                "No sensors are configured, so nothing is being observed. This is the expected "
                f"state of an idle system; {self._policy.minimum_contributing_sensors} "
                "contributing sensor(s) are required before the picture means anything."
            )
        return (
            f"Only {contributing} of {configured} sensors are contributing, below the "
            f"{self._policy.minimum_contributing_sensors} this site requires. The picture is "
            "incomplete and tracks may be coasting on data that has stopped arriving."
        )

    def _record_condition_changes(self, observations: list[SensorObservation]) -> None:
        """One event per sensor whose condition actually moved.

        Only changes: a sensor reporting OK every cycle at 2 Hz would otherwise fill the log
        with the fact that nothing happened.
        """
        for observation in observations:
            previous = self._conditions.get(observation.sensor_id)
            if previous == observation.condition:
                continue

            self._conditions[observation.sensor_id] = observation.condition
            self._events.append(
                EventType.SENSOR_STATUS_CHANGED,
                SOURCE,
                payload={
                    "sensor_id": observation.sensor_id,
                    "sensor_type": str(observation.sensor_type),
                    "condition": str(observation.condition),
                    "previous_condition": str(previous) if previous else None,
                    "reason": observation.reason,
                    "silent_for_s": observation.silent_for_s,
                    "detections_emitted": observation.detections_emitted,
                },
            )

    def _record_degraded_transition(self, health: SystemHealth) -> None:
        if health.degraded == self._degraded:
            return

        self._degraded = health.degraded
        self._events.append(
            EventType.DEGRADED_MODE_ENTERED if health.degraded else EventType.DEGRADED_MODE_EXITED,
            SOURCE,
            payload={
                "reason": health.reason,
                "contributing_sensors": health.contributing_sensors,
                "configured_sensors": health.configured_sensors,
                "anomalous": [
                    {"sensor_id": s.sensor_id, "condition": str(s.condition), "reason": s.reason}
                    for s in health.anomalous
                ],
            },
        )
