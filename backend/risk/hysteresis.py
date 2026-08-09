"""Score to level, with the lag that stops an operator being spammed.

Implements ADR 0007 exactly: dual rise/fall thresholds, plus an asymmetric minimum dwell.
Both mechanisms must permit a change, because they solve different problems — dual thresholds
handle a score drifting slowly across a boundary, minimum dwell handles one jumping wildly
across a wide band, and neither stops the other's case.

The score itself is never smoothed. Hysteresis applies to the discrete level and therefore to
alerting; an operator looking at the number sees what the engine actually computed this cycle.
Smoothing it would make the displayed figure disagree with the reason codes attached to it,
and the reason codes are the part that is meant to be actionable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from backend.tracking.models import RiskLevel

# Ascending, so a scan for "the highest band whose rise threshold is met" is a walk down this
# list. NONE is absent by construction: it is the floor, reached by falling out of LOW.
_ASCENDING = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)


class LevelBand(BaseModel):
    """One level's entry and exit thresholds.

    `fall` sits below `rise`; the gap between them is the hysteresis band. Validated rather
    than assumed, because inverting them by accident produces a system that changes level on
    every cycle in exactly the situation the mechanism exists to prevent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: RiskLevel
    rise: int = Field(ge=0, le=100)
    fall: int = Field(ge=0, le=100)

    def model_post_init(self, _context: object) -> None:
        if self.fall > self.rise:
            raise ValueError(
                f"{self.level}: fall threshold {self.fall} is above rise threshold {self.rise}; "
                "the level could then be entered and left at the same score"
            )


class HysteresisPolicy(BaseModel):
    """Thresholds and dwell times. Configuration, never inline (ADR 0007)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bands: list[LevelBand]

    escalate_dwell_s: float = Field(default=2.0, ge=0.0)
    """Minimum time at a level before escalating away from it. Deliberately short — a genuine
    threat must not be held back by a timer — but non-zero, so one noisy update cannot promote
    a benign object."""

    deescalate_dwell_s: float = Field(default=20.0, ge=0.0)
    """Minimum time before de-escalating. An order of magnitude longer: the cost of holding an
    elevated level too long is an operator looking at something harmless, and the cost of
    dropping it too early is an operator not looking at something that matters."""

    def band(self, level: RiskLevel) -> LevelBand | None:
        return next((band for band in self.bands if band.level == level), None)


@dataclass(slots=True)
class LevelState:
    """Per-track hysteresis state: the current level and when it was last changed."""

    level: RiskLevel
    changed_at: datetime


class LevelResolver:
    """Turns a raw score into a level, given where the track already was."""

    def __init__(self, policy: HysteresisPolicy) -> None:
        self._policy = policy

    def resolve(self, score: int, state: LevelState | None, now: datetime) -> LevelState:
        """The level this track should now be at, and when that was last decided.

        Three gates, in order. A change happens only if all three permit it.

        1. **The hysteresis band.** A track holds its level until the score either reaches a
           higher band's `rise` threshold or drops below its own `fall` threshold. Between
           those it does not move, whatever the raw score is doing.
        2. **Minimum dwell**, asymmetric by direction.
        3. Nothing else — in particular the level is *not* stepped one band at a time. The
           dwell timer already bounds how fast it can move, and stepping would make a score
           jumping from 10 to 95 take four cycles to be believed, which is the alert arriving
           late for the clearest case in the system.

        A track with no prior state adopts its target level immediately: there is nothing to
        flap against on a first assessment, and making a new track climb the bands would delay
        the alert on exactly the case — something appearing already close and closing — that
        most needs it now.
        """
        target = self.target_level(score)
        if state is None:
            return LevelState(level=target, changed_at=now)
        if target == state.level:
            return state

        escalating = target.severity > state.level.severity
        if not escalating and not self.would_leave(state.level, score):
            # Inside the hysteresis band: the score has drifted but not far enough to mean
            # anything.
            return state

        if not self._holds_long_enough(state, now, escalating):
            return state

        return LevelState(level=target, changed_at=now)

    def target_level(self, score: int) -> RiskLevel:
        """The level the score alone implies, before band and dwell are considered.

        Uses `rise` only. Leaving a level is governed by `would_leave`, so a score sitting
        inside the hysteresis band holds whatever level the track already has rather than
        being re-derived from scratch every cycle.
        """
        for level in reversed(_ASCENDING):
            band = self._policy.band(level)
            if band is not None and score >= band.rise:
                return level
        return RiskLevel.NONE

    def would_leave(self, level: RiskLevel, score: int) -> bool:
        """Whether a score has fallen far enough to leave a level it currently holds."""
        band = self._policy.band(level)
        return band is not None and score < band.fall

    def _holds_long_enough(self, state: LevelState, now: datetime, escalating: bool) -> bool:
        required = self._policy.escalate_dwell_s if escalating else self._policy.deescalate_dwell_s
        return now - state.changed_at >= timedelta(seconds=required)
