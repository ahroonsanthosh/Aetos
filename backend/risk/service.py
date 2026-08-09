"""Running risk assessment over a tracking cycle's output.

Sits after classification: takes the tracks a cycle produced, assembles what each one looks
like to the risk engine, scores it, applies hysteresis, and records a `RISK_CHANGED` event
wherever the level actually moved.

Two pieces of per-track state live here rather than in the engine, because both are histories
and the engine is a pure function of one moment:

- **Hysteresis state** — the current level and when it last changed (ADR 0007).
- **Loiter memory** — a bounded trail of recent positions, so path length can be compared with
  net displacement. Nothing else in the factor set can see an object holding station.

Like classification, this module computes a value it does not own. The result is written back
through a sink so the tracker's snapshot carries it and the log replays into the same state
(ADR 0006, FM-025).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from backend.decision.recommend import recommend
from backend.events.store import EventStore
from backend.events.types import EventType
from backend.risk.engine import RiskEngine, RiskPolicy
from backend.risk.factors import TrackRisk
from backend.risk.hysteresis import LevelResolver, LevelState
from backend.risk.models import RiskAssessment
from backend.tracking.models import ThreatFactor, Track, TrackThreat
from backend.tracking.track_manager.manager import TRACK_SNAPSHOT_KEY
from backend.zones.geometry import EnuPoint, EnuVelocity, SiteGeometry

SOURCE = "risk"

_LOITER_MEMORY = 120
"""Positions remembered per track for the loiter factor.

Bounded because a long-lived track would otherwise accumulate one entry per cycle forever, and
because loitering is a *recent* behaviour — an object that hovered for two minutes and then
left should stop reading as loitering, which an unbounded window would never let it do.
"""

_SIGNIFICANT_SCORE_SHIFT = 8
"""Score movement, with the level unchanged, that still warrants recording.

Level changes are always recorded. Between them the score drifts continuously, and an event on
every cycle would make the log unreadable — but a score climbing from 30 to 65 inside MEDIUM is
a real development an operator reviewing the history should be able to see.
"""


@dataclass(frozen=True, slots=True)
class _Sample:
    at: datetime
    east_m: float
    north_m: float


class RiskSink(Protocol):
    """Where a finished assessment is written back to.

    Narrowed to the one method rather than typed as `TrackManager`, so this module depends on
    a capability instead of on the tracker.
    """

    def apply_threat(self, track_id: str, threat: TrackThreat) -> Track | None:
        """Store the assessment, returning the updated track, or None if it is gone."""
        ...


class RiskService:
    """Assesses a cycle's tracks and records what changed."""

    def __init__(
        self,
        engine: RiskEngine,
        policy: RiskPolicy,
        geometry: SiteGeometry,
        events: EventStore,
        sink: RiskSink,
    ) -> None:
        self._engine = engine
        self._geometry = geometry
        self._events = events
        self._sink = sink
        self._resolver = LevelResolver(policy.hysteresis)

        self._levels: dict[str, LevelState] = {}
        self._trails: dict[str, deque[_Sample]] = {}
        self._last: dict[str, RiskAssessment] = {}

    def assess_cycle(self, tracks: list[Track], now: datetime) -> list[Track]:
        """Score each track, returning the tracks with risk applied."""
        assessed: list[Track] = []

        for track in tracks:
            self._remember(track, now)
            raw = self._engine.assess(self._describe(track, now), self._geometry)

            state = self._resolver.resolve(raw.score, self._levels.get(track.track_id), now)
            self._levels[track.track_id] = state
            assessment = raw.model_copy(update={"level": state.level})

            stored = self._sink.apply_threat(track.track_id, _to_track_threat(assessment))
            if stored is None:
                # Retired between tracking and assessment. Nothing to record about a track
                # that no longer exists.
                continue

            assessed.append(stored)

            if self._is_significant(track.track_id, assessment):
                self._record(stored, assessment)
                self._last[track.track_id] = assessment

        self._forget_absent(tracks)
        return assessed

    def _describe(self, track: Track, now: datetime) -> TrackRisk:
        """Reduce a track to exactly what the engine is allowed to see.

        Deliberately not the `Track` itself. Passing the whole thing would let a factor start
        reading the *previous* risk level, and a score that depends on its own last value stops
        being evidence of anything.
        """
        path_length, net_displacement, window_s = self._loiter_geometry(track.track_id)

        return TrackRisk(
            track_id=track.track_id,
            position=EnuPoint(track.enu.east_m, track.enu.north_m, track.enu.up_m),
            velocity=EnuVelocity(
                track.enu.velocity_east_mps,
                track.enu.velocity_north_mps,
                track.enu.velocity_up_mps,
            ),
            speed_mps=track.kinematics.speed_mps,
            altitude_m=track.position.altitude_m,
            detection_count=track.tracking.detection_count,
            track_age_s=(now - track.tracking.first_seen).total_seconds(),
            coasting=track.tracking.is_coasting,
            track_confidence=track.tracking.track_confidence,
            classification=dict(track.classification.distribution),
            classification_confidence=track.classification.confidence,
            classification_assessed=track.classification.assessed,
            path_length_m=path_length,
            net_displacement_m=net_displacement,
            loiter_window_s=window_s,
        )

    def _remember(self, track: Track, now: datetime) -> None:
        trail = self._trails.setdefault(track.track_id, deque(maxlen=_LOITER_MEMORY))
        trail.append(_Sample(at=now, east_m=track.enu.east_m, north_m=track.enu.north_m))

    def _loiter_geometry(self, track_id: str) -> tuple[float, float, float]:
        """Path length, net displacement, and the window both cover.

        The ratio between the first two is what separates holding station from transiting: a
        closed path accumulates length without displacement, a straight one accumulates both
        equally.
        """
        trail = self._trails.get(track_id)
        if trail is None or len(trail) < 2:
            return 0.0, 0.0, 0.0

        samples = list(trail)
        path = sum(
            math.dist(
                (samples[i].east_m, samples[i].north_m),
                (samples[i + 1].east_m, samples[i + 1].north_m),
            )
            for i in range(len(samples) - 1)
        )
        net = math.dist(
            (samples[0].east_m, samples[0].north_m),
            (samples[-1].east_m, samples[-1].north_m),
        )
        window = (samples[-1].at - samples[0].at).total_seconds()
        return path, net, window

    def _is_significant(self, track_id: str, assessment: RiskAssessment) -> bool:
        previous = self._last.get(track_id)
        if previous is None:
            # Never recorded. Worth an event only once there is something to say — otherwise
            # every track emits a "still nothing" entry on its first cycle.
            return assessment.score > 0 or assessment.level != assessment.level.NONE

        if previous.level != assessment.level:
            return True
        return abs(assessment.score - previous.score) >= _SIGNIFICANT_SCORE_SHIFT

    def _record(self, track: Track, assessment: RiskAssessment) -> None:
        previous = self._last.get(track.track_id)
        self._events.append(
            EventType.RISK_CHANGED,
            SOURCE,
            payload={
                "score": assessment.score,
                "level": str(assessment.level),
                # Both levels, as ADR 0007 requires: a level change is only interpretable
                # against what it changed from.
                "previous_level": str(previous.level) if previous else None,
                "previous_score": previous.score if previous else None,
                "confidence": assessment.confidence,
                "mitigation": assessment.mitigation,
                "codes": assessment.codes,
                "factors": [
                    {
                        "code": factor.code,
                        "weight": round(factor.weight, 4),
                        "activation": round(factor.activation, 4),
                        "contribution": round(factor.contribution, 4),
                        "detail": factor.detail,
                    }
                    for factor in assessment.factors
                ],
                TRACK_SNAPSHOT_KEY: track.model_dump(mode="json"),
            },
            track_id=track.track_id,
        )

    def _forget_absent(self, tracks: list[Track]) -> None:
        """Drop everything remembered about tracks that are no longer present."""
        present = {track.track_id for track in tracks}
        for store in (self._levels, self._trails, self._last):
            for track_id in [key for key in store if key not in present]:
                del store[track_id]


def _to_track_threat(assessment: RiskAssessment) -> TrackThreat:
    return TrackThreat(
        risk_score=assessment.score,
        risk_level=assessment.level,
        confidence=assessment.confidence,
        reasons=assessment.reasons,
        codes=assessment.codes,
        factors=[
            ThreatFactor(
                code=factor.code,
                weight=factor.weight,
                activation=factor.activation,
                contribution=factor.contribution,
                detail=factor.detail,
            )
            for factor in assessment.factors
        ],
        recommendation=recommend(assessment),
        assessed=assessment.assessed,
    )
