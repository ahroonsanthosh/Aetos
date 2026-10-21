"""What an alert is, and the states it can be in.

An alert is a risk finding that has been **promoted**: given an identity, a lifecycle, and a
claim on an operator's attention that outlives the cycle it was raised in. That last part is
the whole point. A risk score is a property of one moment — recomputed every cycle, replaced
without ceremony, and gone the instant the geometry changes. An alert persists until someone
or something ends it, which is what makes it possible to say "this was raised, and nobody
looked at it" (spec §11, ADR 0015).

Alerting deliberately owns no assessment logic. It reads the level risk already decided and
decides only whether that level warrants interrupting a human. Keeping the two apart means a
change to what counts as alertable cannot quietly change what counts as dangerous.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from backend.tracking.models import RiskLevel

ALERT_SCHEMA_VERSION = "1.0.0"


class AlertState(StrEnum):
    """Where an alert is in its life.

    Acknowledged is a distinct state from cleared, and the distinction is the reason this
    module exists. Acknowledging says a human has *seen* the alert; clearing says the
    condition that raised it has *ended*. Collapsing them would let an operator dismiss a
    still-approaching aircraft and leave no record that the threat outlived the dismissal.
    """

    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    CLEARED = "cleared"

    @property
    def open(self) -> bool:
        """Whether the alert is still live. Acknowledged alerts are."""
        return self is not AlertState.CLEARED


class ClearReason(StrEnum):
    """Why an alert ended.

    Recorded because "the aircraft turned away" and "we stopped being able to see it" are
    very different facts, and an alert feed that renders them identically invites the reader
    to assume the reassuring one.
    """

    RISK_SUBSIDED = "risk_subsided"
    """The track's risk level fell below the clearing threshold and stayed there."""

    TRACK_LOST = "track_lost"
    """The track disappeared — out of coverage, or dropped by the tracker. The object may
    still be out there; the system simply cannot see it any more."""

    DUPLICATE_TRACK = "duplicate_track"
    """The track was a second track on an object another track already follows, and the
    tracker retired it (ADR 0021). Distinct from TRACK_LOST on purpose: that one tells an
    operator something may still be out there unwatched, and here nothing is — the incident
    continues under the surviving track, which the event names."""


class Alert(BaseModel):
    """One raised alert, at one point in its life.

    Frozen, and advanced by producing a new instance. The log is the authority on the
    sequence of states an alert passed through (ADR 0006); an in-place mutation would leave
    the current object correct and the history unreconstructable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_id: str
    track_id: str
    state: AlertState

    raised_at: datetime
    raised_level: RiskLevel
    """The level that crossed the threshold. Kept alongside `peak_level` because an alert
    that opened at HIGH and one that escalated into it from MEDIUM are different stories."""

    peak_level: RiskLevel
    """The worst level reached while open. Never falls — an alert that touched CRITICAL and
    subsided is not retrospectively a MEDIUM one, and the operator reviewing it later needs
    to know what it was at its worst."""

    level: RiskLevel
    """The level as of the most recent cycle. Falls freely; `peak_level` does not."""

    score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0.0, le=1.0)

    codes: list[str] = Field(default_factory=list)
    """Risk's machine-readable reason codes, carried through unchanged. Alerting does not
    invent its own vocabulary for why something is dangerous — it has no opinion on that."""

    reasons: list[str] = Field(default_factory=list)
    """The human-readable sentences behind `codes`, in the same order. Every alert ships with
    these for the same reason every risk score does: an alert an operator cannot interrogate
    is one they can only obey or ignore (spec §10)."""

    updated_at: datetime
    escalated_at: datetime | None = None

    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None

    cleared_at: datetime | None = None
    clear_reason: ClearReason | None = None

    schema_version: str = ALERT_SCHEMA_VERSION

    @property
    def open(self) -> bool:
        return self.state.open

    @property
    def acknowledged(self) -> bool:
        return self.acknowledged_at is not None

    @property
    def escalated(self) -> bool:
        """Whether this alert ever rose above the level it was raised at."""
        return self.peak_level.severity > self.raised_level.severity
