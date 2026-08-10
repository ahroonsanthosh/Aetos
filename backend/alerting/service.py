"""Promoting risk levels into alerts, and running their lifecycle.

Sits at the end of the pipeline, after risk. Its input is the assessed track — specifically
`track.threat`, which risk has already written back — not the risk engine. That indirection is
the point: alerting is a *consumer* of assessments on exactly the same terms as the API is, so
it cannot reach into how a score was reached and start second-guessing it.

What this module decides is narrow: whether a level warrants interrupting a human, and when
that interruption ends. Everything it says about *why* is risk's vocabulary, carried through
unchanged.

State lives in memory and is rebuilt from the log at startup (`rebuild`). The log is the
authority (ADR 0006); this is a projection of it. An open alert surviving a restart is not a
nicety — an alert that quietly vanished because the process bounced is indistinguishable, to
the operator, from one that was dealt with.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.alerting.models import Alert, AlertState, ClearReason
from backend.alerting.policy import AlertPolicy
from backend.common.identifiers import alert_id_generator, alert_id_sequence
from backend.events.store import EventStore
from backend.events.types import EventType
from backend.tracking.models import Track

SOURCE = "alerting"

ALERT_ID_KEY = "alert_id"
"""Payload key naming the alert an event refers to.

Every alert event after the first carries it, because `ALERT_CREATED` is identified by the
alert's own ID rather than by the event's — an alert outlives the event that raised it, and
keying its later events off the creating event's ID would make the payload's meaning depend on
which event you happened to read first.
"""

ALERT_KEY = "alert"
"""Payload key holding the full alert record, so replay can reconstruct without joining."""


@dataclass(slots=True)
class _Subsiding:
    """How long an open alert's level has been at or below the clearing threshold."""

    since: datetime


class AlertService:
    """Raises, escalates, acknowledges and clears alerts."""

    def __init__(self, policy: AlertPolicy, events: EventStore) -> None:
        self._policy = policy
        self._events = events
        self._ids = alert_id_generator()

        self._open: dict[str, Alert] = {}
        """Open alerts by alert_id. Acknowledged alerts are still open."""

        self._by_track: dict[str, str] = {}
        """track_id -> alert_id, for the one open alert a track may have.

        One at a time, deliberately. A track whose risk rises, falls and rises again produces
        two alerts in sequence, which is the truth; a track holding at HIGH produces one, not
        one per cycle.
        """

        self._subsiding: dict[str, _Subsiding] = {}

    # -----------------------------------------------------------------------------------
    # Reading
    # -----------------------------------------------------------------------------------

    @property
    def active(self) -> list[Alert]:
        """Open alerts, worst first, then oldest first.

        Ordered here rather than by the caller because "which alert matters most" is a
        property of the alert set, and two consumers sorting it differently would show two
        operators different top-of-list items from the same data.
        """
        return sorted(
            self._open.values(),
            key=lambda alert: (-alert.peak_level.severity, -alert.score, alert.raised_at),
        )

    def get(self, alert_id: str) -> Alert | None:
        return self._open.get(alert_id)

    # -----------------------------------------------------------------------------------
    # The cycle
    # -----------------------------------------------------------------------------------

    def review_cycle(self, tracks: list[Track], now: datetime) -> list[Alert]:
        """Bring the alert set into line with this cycle's assessments.

        Returns the alerts that changed state, so a caller can react without re-diffing.
        """
        changed: list[Alert] = []
        present: set[str] = set()

        for track in tracks:
            if not track.threat.assessed:
                # Risk has not reached this track yet. Not alertable, and not evidence that
                # it is safe — the absence of an assessment is not an assessment of absence.
                continue

            present.add(track.track_id)
            altered = self._review_track(track, now)
            if altered is not None:
                changed.append(altered)

        changed.extend(self._clear_absent(present, now))
        return changed

    def _review_track(self, track: Track, now: datetime) -> Alert | None:
        level = track.threat.risk_level
        alert_id = self._by_track.get(track.track_id)

        if alert_id is None:
            if level.severity >= self._policy.raise_at.severity:
                return self._raise(track, now)
            return None

        alert = self._open[alert_id]
        updated = alert.model_copy(
            update={
                "level": level,
                "score": track.threat.risk_score,
                "confidence": track.threat.confidence,
                "codes": list(track.threat.codes),
                "reasons": list(track.threat.reasons),
                "updated_at": now,
            }
        )
        self._open[alert_id] = updated

        if level.severity > alert.peak_level.severity:
            return self._escalate(updated, now)

        if level.severity <= self._policy.clear_at_or_below.severity:
            return self._maybe_clear(updated, now)

        # Back inside the band between the two thresholds: no longer subsiding, not
        # escalating. Resetting here is what makes the dwell mean "continuously down" rather
        # than "down at some point in the last twenty seconds".
        self._subsiding.pop(alert_id, None)
        return None

    def _maybe_clear(self, alert: Alert, now: datetime) -> Alert | None:
        subsiding = self._subsiding.get(alert.alert_id)
        if subsiding is None:
            self._subsiding[alert.alert_id] = _Subsiding(since=now)
            return None

        if (now - subsiding.since).total_seconds() < self._policy.clear_dwell_s:
            return None

        return self._clear(alert, ClearReason.RISK_SUBSIDED, now)

    def _clear_absent(self, present: set[str], now: datetime) -> list[Alert]:
        """Clear alerts whose track is no longer in the picture."""
        if not self._policy.clear_on_track_lost:
            return []

        missing = [
            (track_id, alert_id)
            for track_id, alert_id in self._by_track.items()
            if track_id not in present
        ]
        return [
            self._clear(self._open[alert_id], ClearReason.TRACK_LOST, now)
            for _, alert_id in missing
        ]

    # -----------------------------------------------------------------------------------
    # Transitions
    # -----------------------------------------------------------------------------------

    def _raise(self, track: Track, now: datetime) -> Alert:
        alert = Alert(
            alert_id=self._ids.next(),
            track_id=track.track_id,
            state=AlertState.ACTIVE,
            raised_at=now,
            raised_level=track.threat.risk_level,
            peak_level=track.threat.risk_level,
            level=track.threat.risk_level,
            score=track.threat.risk_score,
            confidence=track.threat.confidence,
            codes=list(track.threat.codes),
            reasons=list(track.threat.reasons),
            updated_at=now,
        )
        self._remember(alert)
        self._record(EventType.ALERT_CREATED, alert)
        return alert

    def _escalate(self, alert: Alert, now: datetime) -> Alert:
        """Raise the peak, and drop any acknowledgement.

        The acknowledgement is dropped on purpose. It attested that a human had seen *this
        alert as it then stood*; the situation has since got worse, and carrying the
        acknowledgement forward would let a MEDIUM someone glanced at become a CRITICAL
        nobody has looked at, while still displaying as handled.
        """
        escalated = alert.model_copy(
            update={
                "peak_level": alert.level,
                "escalated_at": now,
                "state": AlertState.ACTIVE,
                "acknowledged_at": None,
                "acknowledged_by": None,
            }
        )
        self._remember(escalated)
        self._subsiding.pop(alert.alert_id, None)
        self._record(
            EventType.ALERT_ESCALATED,
            escalated,
            extra={
                "previous_level": str(alert.peak_level),
                "acknowledgement_withdrawn": alert.acknowledged,
            },
        )
        return escalated

    def acknowledge(self, alert_id: str, operator_id: str, now: datetime) -> Alert | None:
        """Record that a human has seen this alert. Does not clear it.

        Returns None if there is no such open alert, so the API can answer 404 rather than
        inventing one. Acknowledging twice is not an error — it re-stamps the record, which
        is the least surprising behaviour for a button an operator may press twice.
        """
        alert = self._open.get(alert_id)
        if alert is None:
            return None

        acknowledged = alert.model_copy(
            update={
                "state": AlertState.ACKNOWLEDGED,
                "acknowledged_at": now,
                "acknowledged_by": operator_id,
            }
        )
        self._remember(acknowledged)
        self._events.append(
            EventType.OPERATOR_ACKNOWLEDGED,
            SOURCE,
            payload={
                ALERT_ID_KEY: alert_id,
                "operator_id": operator_id,
                "level": str(acknowledged.level),
                "peak_level": str(acknowledged.peak_level),
            },
            track_id=alert.track_id,
        )
        return acknowledged

    def _clear(self, alert: Alert, reason: ClearReason, now: datetime) -> Alert:
        cleared = alert.model_copy(
            update={"state": AlertState.CLEARED, "cleared_at": now, "clear_reason": reason}
        )
        self._open.pop(alert.alert_id, None)
        self._by_track.pop(alert.track_id, None)
        self._subsiding.pop(alert.alert_id, None)

        self._record(
            EventType.ALERT_CLEARED,
            cleared,
            extra={
                "reason": str(reason),
                "acknowledged": alert.acknowledged,
                "open_duration_s": round((now - alert.raised_at).total_seconds(), 3),
            },
        )
        return cleared

    def _remember(self, alert: Alert) -> None:
        self._open[alert.alert_id] = alert
        self._by_track[alert.track_id] = alert.alert_id

    def _record(
        self, event_type: EventType, alert: Alert, extra: dict[str, object] | None = None
    ) -> None:
        payload: dict[str, object] = {
            ALERT_ID_KEY: alert.alert_id,
            ALERT_KEY: alert.model_dump(mode="json"),
            "level": str(alert.level),
            "peak_level": str(alert.peak_level),
            "score": alert.score,
            "codes": list(alert.codes),
        }
        payload.update(extra or {})
        self._events.append(event_type, SOURCE, payload=payload, track_id=alert.track_id)

    # -----------------------------------------------------------------------------------
    # Rebuilding from the log
    # -----------------------------------------------------------------------------------

    def rebuild(self) -> int:
        """Reconstruct open alerts by replaying the log. Returns how many are open.

        Called once at startup, before the first cycle. Without it a restart silently
        discharges every open alert — the operator sees an empty feed and has no way to tell
        that from a quiet sky.

        The identifier generator is resumed above the highest ID the log contains, so a
        restart cannot mint a second alert with an existing ID.
        """
        self._open.clear()
        self._by_track.clear()
        self._subsiding.clear()

        highest = 0

        for event in self._events.read_from(1):
            alert_id = event.payload.get(ALERT_ID_KEY)
            if not isinstance(alert_id, str):
                continue

            sequence = alert_id_sequence(alert_id)
            if sequence is not None:
                highest = max(highest, sequence)

            if event.event_type in (EventType.ALERT_CREATED, EventType.ALERT_ESCALATED):
                recorded = event.payload.get(ALERT_KEY)
                if isinstance(recorded, dict):
                    self._remember(Alert.model_validate(recorded))
            elif event.event_type is EventType.OPERATOR_ACKNOWLEDGED:
                existing = self._open.get(alert_id)
                if existing is not None:
                    operator = event.payload.get("operator_id")
                    self._remember(
                        existing.model_copy(
                            update={
                                "state": AlertState.ACKNOWLEDGED,
                                "acknowledged_at": event.timestamp,
                                "acknowledged_by": operator if isinstance(operator, str) else None,
                            }
                        )
                    )
            elif event.event_type is EventType.ALERT_CLEARED:
                closing = self._open.pop(alert_id, None)
                if closing is not None:
                    self._by_track.pop(closing.track_id, None)

        self._ids = alert_id_generator(start=highest + 1)
        return len(self._open)
