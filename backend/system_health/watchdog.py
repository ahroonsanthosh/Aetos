"""The watchdog: deciding whether the system is healthy, degraded, or lying to its operator.

## What this adds that the adapters cannot

Every adapter reports on itself, honestly and cheaply, and stops there. `SensorHealth` will
tell you a port could not be opened or that a byte stream is not framing. What no adapter can
tell you is whether the silence it is reporting means anything, because that judgement needs
two things the adapter does not have: a **window** — history across cycles — and a
**site-level policy** saying how long this kind of source may reasonably say nothing.

This module holds both. It is the only place in the system permitted to turn "no detections"
into a statement, and the statement it is permitted to make is deliberately weak: *this source
has been in contact and silent for longer than this site expects.* Never "this source has
failed". An empty sky produces exactly the same evidence, and the honest thing is to report
the fact and the threshold and let the operator decide. That distinction is FM-010.

## What it detects that nothing else can

The stall. Every component here reports on itself, and a component that has stopped running
reports nothing — which renders identically to a component with nothing to say. The watchdog's
last pass is timestamped, and `report()` compares that timestamp against the clock at the
moment somebody asks. A pipeline that has stopped stepping therefore shows as STALLED to the
API even though nothing inside the pipeline is in any position to say so. That is the
self-test, and it works precisely because it is evaluated outside the thing it is testing.

## What it does not do

It does not change behaviour. Degraded mode here is a *declaration*, recorded in the log and
shown on the display; no threshold moves and no track is treated differently because of it.
That is on purpose — silently altering the assessment pipeline when the picture thins would
mean an operator comparing two incidents was comparing two different systems. The declaration
is the product.
"""

from __future__ import annotations

import logging
from datetime import datetime

from backend.common.clock import Clock
from backend.events.store import EventStore
from backend.events.types import EventType
from backend.system_health.models import (
    HealthAssessment,
    Posture,
    SensorAssessment,
    SensorVerdict,
)
from backend.system_health.policy import SystemHealthPolicy
from sensors.adapters.interface import SensorHealth, SensorState

_LOG = logging.getLogger(__name__)

SOURCE = "system_health.watchdog"
"""What the events it appends are attributed to. One constant rather than a literal at each
call site — the same fix as `OPERATOR_ID_KEY` in alerting, and for the same reason: two spellings
of one name is the bug this repository keeps finding."""


class Watchdog:
    """Judges sensor health over a window and declares the system's posture.

    Stateful, and the state is the point: it holds first-seen times and previous verdicts,
    which is exactly what the per-request health row cannot have. Stepped once per pipeline
    cycle by whoever owns the pipeline, and read at any time by the API.

    Single-threaded with respect to the pipeline, like every other cycle stage. `report()` is
    read-only and safe to call from the request path.
    """

    def __init__(self, clock: Clock, policy: SystemHealthPolicy, events: EventStore) -> None:
        self._clock = clock
        self._policy = policy
        self._events = events

        self._first_seen: dict[str, datetime] = {}
        """When this watchdog first saw each source, used for the startup grace window.

        The watchdog's own observation rather than the adapter's start time, because an adapter
        does not report one — and inventing a field for it would put the same fact in two
        places, free to disagree.
        """

        self._verdicts: dict[str, SensorVerdict] = {}
        """Last verdict per source. Events are emitted on transition, not per cycle: a log
        that recorded a healthy radar twice a second would bury the moment it stopped."""

        self._healthy_since: dict[str, datetime] = {}
        """When each source last *began* satisfying its threshold continuously.

        The hysteresis state. A source whose verdict was bad keeps it until this has stood for
        `recovery_dwell_windows`, which is what stops a marginal sensor alternating between two
        verdicts forever. Cleared the moment the source stops satisfying the threshold, so the
        dwell measures sustained recovery rather than elapsed time since anything.
        """

        self._posture: Posture = Posture.NOMINAL
        self._latest: HealthAssessment | None = None

    @property
    def policy(self) -> SystemHealthPolicy:
        """The thresholds this watchdog is judging against. Read by the wiring, which checks
        them against the pipeline tick, and by tests."""
        return self._policy

    @property
    def latest(self) -> HealthAssessment | None:
        """The last pass, unaged. None before the first cycle."""
        return self._latest

    def assess(self, reports: list[SensorHealth]) -> HealthAssessment:
        """Judge every source, declare the posture, and record what changed.

        Called once per cycle with whatever the pipeline's `sensor_health()` returned — which
        has already overridden a self-reporting adapter that raised on its last poll. This
        method judges those facts; it does not gather them.
        """
        now = self._clock.now()
        assessments = [self._assess_one(report, now) for report in reports if self._judged(report)]

        posture = self._posture_for(assessments, considered=len(reports))
        assessment = HealthAssessment(
            assessed_at=now,
            posture=posture,
            summary=_summarise(posture, assessments, considered=len(reports)),
            sensors=assessments,
        )

        self._record_sensor_transitions(assessments)
        self._record_posture_transition(assessment)
        self._latest = assessment
        return assessment

    def report(self) -> HealthAssessment:
        """The current assessment, aged against the clock — the self-test.

        Not simply `latest`. If the last pass is older than `stall_threshold_s`, the pipeline
        has stopped stepping and the returned posture is STALLED regardless of what the sensors
        said the last time anybody asked them. A caller that read `latest` directly would be
        served a stale green light by a dead pipeline, which is the exact failure this module
        exists to make impossible.

        Read-only: it appends nothing and mutates nothing, so the request path can call it.
        """
        if self._latest is None:
            # Genuinely unknown: this watchdog has never been given anything to judge, so it
            # cannot distinguish an idle deployment from a dead one and must not guess. The
            # wiring assesses once at startup precisely so a running system never lands here.
            return HealthAssessment(
                assessed_at=self._clock.now(),
                posture=Posture.STALLED,
                summary="The watchdog has not completed a cycle. Nothing is being monitored.",
            )

        age_s = (self._clock.now() - self._latest.assessed_at).total_seconds()
        if age_s <= self._policy.stall_threshold_s:
            return self._latest

        # Nothing was attached, so nothing was expected to arrive, and an assessment about no
        # sources cannot go out of date. Saying STALLED here would raise a fault on an ordinary
        # start — a deployment with no scenario and no hardware genuinely has no cycle to run —
        # and BLIND is both true and the more actionable of the two. Staleness is a claim that
        # observations *stopped*; it needs something to have been observing.
        if not self._latest.sensors:
            return self._latest

        return self._latest.model_copy(
            update={
                "posture": Posture.STALLED,
                "summary": (
                    f"The pipeline has not stepped for {age_s:.1f}s, beyond the "
                    f"{self._policy.stall_threshold_s:.0f}s stall threshold. Sensor states below "
                    "are from the last completed cycle and are no longer current."
                ),
            }
        )

    def _judged(self, report: SensorHealth) -> bool:
        if report.sensor_type.is_simulated:
            return self._policy.assess_simulated_sensors
        return True

    def _assess_one(self, report: SensorHealth, now: datetime) -> SensorAssessment:
        threshold = self._policy.threshold_for(report.sensor_type)
        first_seen = self._first_seen.setdefault(report.sensor_id, now)
        silent_for = (
            (now - report.last_detection_time).total_seconds()
            if report.last_detection_time is not None
            else None
        )

        verdict, reason = self._verdict_for(report, now, first_seen, silent_for, threshold)
        return SensorAssessment(
            sensor_id=report.sensor_id,
            sensor_type=report.sensor_type,
            verdict=verdict,
            reason=reason,
            seconds_since_detection=None if silent_for is None else max(0.0, silent_for),
            silence_threshold_s=threshold,
        )

    def _verdict_for(
        self,
        report: SensorHealth,
        now: datetime,
        first_seen: datetime,
        silent_for: float | None,
        threshold: float,
    ) -> tuple[SensorVerdict, str]:
        """Map one self-report plus the window onto a verdict.

        The adapter's own state is taken at face value and is never overridden upwards: a
        source that says it is offline is offline, and no amount of recent data changes that.
        Only the OK case is examined further, because it is the only one where the adapter's
        account and the data can disagree — and that disagreement is the whole point.
        """
        if report.state is not SensorState.OK:
            # Whatever recovery was under way has ended. Restarting the dwell here rather than
            # letting it run is what makes it measure *sustained* health.
            self._healthy_since.pop(report.sensor_id, None)

        if report.state is SensorState.OFFLINE:
            return SensorVerdict.OFFLINE, f"Not observing: {report.reason}"
        if report.state is SensorState.NOT_STARTED:
            return SensorVerdict.NOT_STARTED, f"Attached but never started: {report.reason}"
        if report.state is SensorState.DEGRADED:
            return SensorVerdict.DEGRADED, f"Reduced capability: {report.reason}"

        # State is OK. Now — and only now — does the data get a say.
        within_grace = (now - first_seen).total_seconds() <= self._policy.startup_grace_s
        if silent_for is None:
            if within_grace:
                return self._recovered_or_still(
                    report,
                    now,
                    "Started and reporting normally; nothing observed yet, still within the "
                    f"{self._policy.startup_grace_s:.0f}s startup grace window.",
                )
            self._healthy_since.pop(report.sensor_id, None)
            return (
                SensorVerdict.QUIET,
                "In contact and healthy by its own account, but has produced no detection "
                "since it started. This is what an empty sky looks like and also what a "
                "sensor pointed at nothing looks like; it is not a fault claim.",
            )

        if silent_for <= threshold:
            return self._recovered_or_still(
                report,
                now,
                f"Reporting normally; last detection {silent_for:.1f}s ago.",
            )

        self._healthy_since.pop(report.sensor_id, None)
        return (
            SensorVerdict.QUIET,
            f"In contact and healthy by its own account, but silent for {silent_for:.1f}s "
            f"against a {threshold:.0f}s threshold for this sensor type. Silence is not "
            "evidence of failure — an empty sky produces it too.",
        )

    def _recovered_or_still(
        self, report: SensorHealth, now: datetime, healthy_reason: str
    ) -> tuple[SensorVerdict, str]:
        """OK — but a QUIET verdict is withdrawn only once the recovery dwell has stood.

        The hysteresis (ADR 0007, and `AlertPolicy.clear_dwell_s` for the same shape in
        alerting). Silence is a sawtooth against a fixed threshold, so without a dwell a source
        whose rate sits anywhere near its window alternates between two verdicts indefinitely —
        a blinking display and hundreds of status events an hour, which bury the one transition
        that mattered.

        A source that never sustains the threshold therefore settles on its bad verdict and
        stays there. That is the honest description of a marginal sensor, and it is what makes
        this hysteresis rather than a delay: the verdict stops moving instead of moving slower.
        """
        previous = self._verdicts.get(report.sensor_id)
        began = self._healthy_since.setdefault(report.sensor_id, now)

        # The dwell guards the verdict this module *infers* — never the fact the adapter
        # *reports*. A source coming back from OFFLINE or DEGRADED has withdrawn its own claim,
        # which is new evidence of exactly the kind that produced the fault, and holding BLIND
        # over a map that is visibly drawing tracks would be a lie in the other direction. If a
        # driver's own state flaps, that is a defect in the driver and belongs in the driver:
        # papering over it here would hide it from the one place it can be fixed.
        if previous is not SensorVerdict.QUIET:
            return SensorVerdict.OK, healthy_reason

        dwell_s = self._policy.recovery_dwell_for(report.sensor_type)
        held_s = (now - began).total_seconds()
        if held_s >= dwell_s:
            return SensorVerdict.OK, f"{healthy_reason} Recovered from {previous.value}."

        return (
            previous,
            f"{healthy_reason} Holding the previous {previous.value} verdict until healthy "
            f"reporting has stood for {dwell_s:.0f}s ({held_s:.1f}s so far) — a verdict that "
            "flipped on the first good cycle would flap on every marginal sensor.",
        )

    def _posture_for(self, assessments: list[SensorAssessment], *, considered: int) -> Posture:
        """Derive the system posture from the individual verdicts.

        Three rules, in order of how bad the news is:

        1. Nothing attached, or nothing observing → BLIND. A display with no sensor behind it
           is memory, and an empty one means nothing at all.
        2. Any source faulted, some still observing → DEGRADED. Not an average: a site with
           four radars and one dead cable is degraded, because the operator's question is never
           "are most of my sensors fine".
        3. Otherwise NOMINAL — including when every source is QUIET, because a calm sky is not
           a fault and declaring degraded mode over one would make the declaration worthless.
        """
        if considered == 0:
            return Posture.BLIND
        if not assessments:
            # Every attached source was excluded by policy. Nothing is being watched by this
            # watchdog, and saying NOMINAL would be a green light nothing computes.
            return Posture.BLIND
        if not any(a.verdict.is_observing for a in assessments):
            return Posture.BLIND
        if any(a.verdict in {SensorVerdict.OFFLINE, SensorVerdict.DEGRADED} for a in assessments):
            return Posture.DEGRADED
        if any(a.verdict is SensorVerdict.NOT_STARTED for a in assessments):
            return Posture.DEGRADED
        return Posture.NOMINAL

    def _record_sensor_transitions(self, assessments: list[SensorAssessment]) -> None:
        """Append SENSOR_STATUS_CHANGED for each source whose verdict moved.

        On transition only. The log is the record of what happened, and something that is
        still the case did not happen again.
        """
        for assessment in assessments:
            previous = self._verdicts.get(assessment.sensor_id)
            if previous == assessment.verdict:
                continue
            self._verdicts[assessment.sensor_id] = assessment.verdict
            self._events.append(
                EventType.SENSOR_STATUS_CHANGED,
                source=assessment.sensor_id,
                payload={
                    "previous": previous.value if previous else None,
                    "verdict": assessment.verdict.value,
                    "reason": assessment.reason,
                    "sensor_type": assessment.sensor_type.value,
                    "seconds_since_detection": assessment.seconds_since_detection,
                    "silence_threshold_s": assessment.silence_threshold_s,
                },
            )
            _LOG.info(
                "Sensor %s: %s -> %s (%s)",
                assessment.sensor_id,
                previous.value if previous else "unknown",
                assessment.verdict.value,
                assessment.reason,
            )

        # A source that has disappeared from the reports is no longer attached. Forgetting it
        # here is what lets it emit a transition again if it comes back, rather than being
        # silently suppressed by a verdict recorded for a pipeline it no longer belongs to.
        present = {a.sensor_id for a in assessments}
        for gone in set(self._verdicts) - present:
            del self._verdicts[gone]
            self._first_seen.pop(gone, None)
            self._healthy_since.pop(gone, None)

    def _record_posture_transition(self, assessment: HealthAssessment) -> None:
        """Append DEGRADED_MODE_ENTERED / EXITED when the posture leaves or regains NOMINAL.

        Both directions are recorded. An entry with no matching exit reads, months later, as a
        system that never recovered — and a recovery nobody logged is indistinguishable from
        one that never happened.
        """
        previous, self._posture = self._posture, assessment.posture
        if previous == assessment.posture:
            return

        if assessment.posture.is_healthy:
            event_type = EventType.DEGRADED_MODE_EXITED
        elif previous.is_healthy:
            event_type = EventType.DEGRADED_MODE_ENTERED
        else:
            # Degraded to blind, or back. Still a change worth recording, and recording it as a
            # fresh entry is honest: the system got worse, and an operator reading the log must
            # not have to infer that from the absence of an event.
            event_type = EventType.DEGRADED_MODE_ENTERED

        self._events.append(
            event_type,
            source=SOURCE,
            payload={
                "previous": previous.value,
                "posture": assessment.posture.value,
                "summary": assessment.summary,
                "sensors": {a.sensor_id: a.verdict.value for a in assessment.sensors},
            },
        )
        _LOG.warning(
            "System posture: %s -> %s. %s",
            previous.value,
            assessment.posture.value,
            assessment.summary,
        )


def _summarise(posture: Posture, assessments: list[SensorAssessment], *, considered: int) -> str:
    """One sentence an operator can act on, naming the sources responsible.

    Names them rather than counting them. "1 of 4 sensors unhealthy" sends someone to the
    display to work out which; the sensor id sends them to the cable.
    """
    if considered == 0:
        return "No sensor adapters are attached. Nothing is observing."
    if not assessments:
        return (
            f"{considered} source(s) attached, none assessed by policy. The watchdog is not "
            "monitoring anything."
        )

    faulted = [a for a in assessments if not a.verdict.is_observing]
    quiet = [a for a in assessments if a.verdict is SensorVerdict.QUIET]
    degraded = [a for a in assessments if a.verdict is SensorVerdict.DEGRADED]

    if posture is Posture.BLIND:
        return (
            f"No source is observing. {_names(faulted)} — the display shows memory, not "
            "observation, and an empty display means nothing."
        )

    if posture is Posture.DEGRADED:
        broken = _names(faulted + degraded)
        return (
            f"{len(faulted) + len(degraded)} of {len(assessments)} source(s) not observing "
            f"normally: {broken}. The remainder are still producing a picture."
        )

    if quiet:
        return (
            f"All {len(assessments)} source(s) in contact. {len(quiet)} silent beyond the "
            f"configured window: {_names(quiet)}. Silence is reported as a fact, not as a fault."
        )

    return f"All {len(assessments)} source(s) in contact and reporting."


def _names(assessments: list[SensorAssessment]) -> str:
    return ", ".join(f"{a.sensor_id} ({a.verdict.value})" for a in assessments) or "none"
