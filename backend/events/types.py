"""Event types and the Event record.

Every state change in Aetos is an event. The log of those events is the source of truth;
in-memory state (active tracks, current alerts) is a projection of it. See docs/adr/0006.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventType(StrEnum):
    """The closed set of things that can happen.

    Closed deliberately: an open string field would let modules invent event types that no
    consumer knows how to interpret, and replay would silently skip them.
    """

    # Ingestion
    DETECTION_RECEIVED = "DETECTION_RECEIVED"
    DETECTION_REJECTED = "DETECTION_REJECTED"
    """A detection failed validation or bounds checks. Recorded rather than dropped, because
    a sensor producing bad data is itself a fact worth being able to reconstruct."""

    DETECTION_SHED = "DETECTION_SHED"
    """Dropped under backpressure rather than because it was invalid. Distinct from
    rejection: the data was fine, the system could not keep up. See ADR 0008."""

    # Track lifecycle
    TRACK_CREATED = "TRACK_CREATED"
    TRACK_UPDATED = "TRACK_UPDATED"
    TRACK_CONFIRMED = "TRACK_CONFIRMED"
    TRACK_COASTING = "TRACK_COASTING"
    """Predicting through a detection gap, not yet demoted."""

    TRACK_STALE = "TRACK_STALE"
    TRACK_LOST = "TRACK_LOST"

    # Assessment
    TRACKS_FUSED = "TRACKS_FUSED"
    """Two or more tracks were reconciled as observations of one object, or that roster
    changed. Recorded on membership change rather than every cycle: the fusion state travels on
    every track snapshot anyway, and what deserves its own entry is the moment the system
    decided two contacts were one (FM-023)."""

    CLASSIFICATION_CHANGED = "CLASSIFICATION_CHANGED"
    RISK_CHANGED = "RISK_CHANGED"

    SITUATION_CHANGED = "SITUATION_CHANGED"
    """A cycle-level finding about several tracks together — a coordinated approach, or the
    display over-counting one object as several. Carries no `track_id`, because it is not about
    any one track; the tracks involved are listed in the payload."""
    ZONE_ENTERED = "ZONE_ENTERED"
    ZONE_EXITED = "ZONE_EXITED"

    # Operator-facing
    ALERT_CREATED = "ALERT_CREATED"
    ALERT_ESCALATED = "ALERT_ESCALATED"
    """An open alert reached a higher risk level than it was raised at. A distinct event
    rather than a second `ALERT_CREATED`, because it is the same incident getting worse — two
    creations would tell an operator reviewing the log that two aircraft came at them."""

    ALERT_CLEARED = "ALERT_CLEARED"
    RECOMMENDATION_CHANGED = "RECOMMENDATION_CHANGED"
    OPERATOR_ACKNOWLEDGED = "OPERATOR_ACKNOWLEDGED"

    # Access control. These are what make the log able to answer "who did this" rather than
    # only "what happened" — before authentication existed, every operator action named an
    # identity the system had never checked (FM-001, ADR 0016).
    #
    # No payload on any of these ever carries a password, a session token, or a hash of
    # either. A failed login records the username that was tried and nothing that was supplied
    # with it; `tests/integration/test_auth_api.py` asserts that rather than trusting it.
    OPERATOR_LOGIN_SUCCEEDED = "OPERATOR_LOGIN_SUCCEEDED"
    OPERATOR_LOGIN_FAILED = "OPERATOR_LOGIN_FAILED"
    """Recorded for a wrong password, an unknown username, and a disabled account alike. The
    payload distinguishes them; the operator-facing response deliberately does not."""

    OPERATOR_LOGGED_OUT = "OPERATOR_LOGGED_OUT"
    OPERATOR_LOCKED_OUT = "OPERATOR_LOCKED_OUT"
    """Failed attempts crossed the policy threshold. Recorded because a lockout an operator
    cannot explain is indistinguishable, from the console, from a system fault."""

    USER_CREATED = "USER_CREATED"
    USER_ROLE_CHANGED = "USER_ROLE_CHANGED"
    USER_ENABLEMENT_CHANGED = "USER_ENABLEMENT_CHANGED"
    USER_PASSWORD_CHANGED = "USER_PASSWORD_CHANGED"
    """That a password changed, never what it changed to."""

    # Scenario control
    SCENARIO_STARTED = "SCENARIO_STARTED"
    SCENARIO_STOPPED = "SCENARIO_STOPPED"

    # System health
    SENSOR_STATUS_CHANGED = "SENSOR_STATUS_CHANGED"
    DEGRADED_MODE_ENTERED = "DEGRADED_MODE_ENTERED"
    DEGRADED_MODE_EXITED = "DEGRADED_MODE_EXITED"
    SYSTEM_STARTED = "SYSTEM_STARTED"

    SUBSCRIBER_FAILED = "SUBSCRIBER_FAILED"
    """A downstream consumer raised while handling a committed event. The event itself
    stands — it happened, and the log's account of it is authoritative regardless of who
    failed to process it. Recorded so the failure is visible rather than swallowed."""


class Event(BaseModel):
    """One thing that happened, at one point in the total order.

    Immutable. The log is append-only: there is no correction mechanism other than appending
    a further event. This is what makes the log defensible as a record — one that can be
    amended is evidence of nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    """Position in the total order, assigned by the single writer.

    This — not `timestamp` — is the authority on what happened before what. Timestamps come
    from sensors whose clocks may disagree (FM-012); `seq` is assigned in one place by one
    writer and is therefore the only ordering that cannot be argued with. Consumers read the
    log by `seq`. See docs/adr/0006."""

    event_id: str = Field(min_length=1, max_length=64)
    timestamp: datetime
    """When the event occurred, from the injected clock (ADR 0003). Never wall time read
    directly, so that replayed events carry the timestamps of the run being replayed."""

    event_type: EventType
    source: str = Field(min_length=1, max_length=128)
    """Which component or sensor produced this. Free-form because it spans modules and
    sensor IDs, but always populated — an event with no attributable source cannot be
    investigated."""

    track_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")
        return v.astimezone(UTC)
