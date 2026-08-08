"""Track lifecycle: creating, maintaining, coasting and retiring tracks.

This module owns the answer to "what does the system currently believe is out there". It runs
one cycle at a time: predict every track forward, ask association which detection belongs to
which track, update the ones that were seen, coast the ones that were not, and move each
track through the lifecycle accordingly.

    NEW → TENTATIVE → CONFIRMED → STALE → LOST

Every transition is an event in the log. A track is never silently deleted — it ends in LOST
and its history is retained (spec §5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from backend.common.clock import Clock
from backend.common.geodesy import EnuPoint, LocalFrame
from backend.common.identifiers import track_id_generator
from backend.events.store import EventStore
from backend.events.types import EventType
from backend.tracking.association.gnn import (
    Candidate,
    GlobalNearestNeighbourAssociator,
    TrackCandidate,
)
from backend.tracking.filters.kalman import (
    ConstantVelocityFilter,
    KalmanState,
    isotropic_covariance,
)
from backend.tracking.models import (
    EnuState,
    Track,
    TrackKinematics,
    TrackMetadata,
    TrackPosition,
    TrackState,
)

SOURCE = "tracking"


@dataclass(frozen=True, slots=True)
class TrackManagerConfig:
    """Lifecycle tuning.

    The asymmetry between promotion and demotion is deliberate and is the main lever on the
    clutter/sensitivity trade-off. Promotion is slow because a confirmed track is a claim the
    operator is asked to believe; demotion of a *tentative* track is fast because unsupported
    tentative tracks are what clutter looks like, and keeping them costs association capacity
    and screen space for no benefit.
    """

    confirm_after_detections: int = 4
    """Detections required before a track is CONFIRMED. Scenario F (clutter) is the test:
    a false detection that never repeats must not reach this count."""

    tentative_max_misses: int = 2
    """Consecutive missed cycles that retire a track still in NEW or TENTATIVE. Short, because
    an unsupported tentative track is most likely clutter."""

    coast_seconds: float = 6.0
    """How long a CONFIRMED track coasts on prediction before being demoted to STALE.

    This is what carries a track through a brief sensor dropout (scenario C) instead of
    fragmenting it into a new track on reacquisition (FM-022). The filter's uncertainty grows
    throughout, so the association gate legitimately widens and a returning detection can be
    recognised as the same object."""

    stale_seconds: float = 12.0
    """How long a STALE track is retained before being declared LOST. It remains visible in
    this state, explicitly marked degraded rather than shown as a confident position."""

    max_active_tracks: int = 200
    """Ceiling on simultaneously maintained tracks. Scenario I's saturation bound made
    explicit: past this, new track initiation is refused and recorded rather than allowed to
    exhaust memory (ADR 0008)."""


@dataclass(slots=True)
class _ActiveTrack:
    """A track together with the filter state the published Track does not expose.

    Split from the immutable `Track` because the filter's mean and covariance are internal
    working state, and publishing them would invite consumers to depend on the estimator's
    representation rather than on the track.
    """

    track_id: str
    state: TrackState
    filter_state: KalmanState
    first_seen: datetime
    last_seen: datetime
    last_updated: datetime
    detection_count: int = 0
    consecutive_misses: int = 0
    contributing_sensors: set[str] = field(default_factory=set)

    def seconds_since_detection(self, now: datetime) -> float:
        return (now - self.last_seen).total_seconds()


@dataclass(frozen=True, slots=True)
class IncomingDetection:
    """A detection reduced to what tracking needs, already converted to the local frame.

    Note what is absent: no speed, no heading, no classification label. Ingestion performs
    that reduction, so the tracker is structurally incapable of consuming the fields ADR 0002
    forbids it from using. The guard test compares runs with and without those fields, and
    they cannot differ if the fields never reach this type.
    """

    detection_id: str
    sensor_id: str
    timestamp: datetime
    position_enu: np.ndarray
    covariance: np.ndarray


class TrackManager:
    """Maintains the set of active tracks across cycles."""

    def __init__(
        self,
        clock: Clock,
        events: EventStore,
        frame: LocalFrame,
        config: TrackManagerConfig | None = None,
        kalman: ConstantVelocityFilter | None = None,
        associator: GlobalNearestNeighbourAssociator | None = None,
    ) -> None:
        self._clock = clock
        self._events = events
        self._frame = frame
        self._config = config or TrackManagerConfig()
        self._kalman = kalman or ConstantVelocityFilter()
        self._associator = associator or GlobalNearestNeighbourAssociator(self._kalman)
        self._ids = track_id_generator()
        self._active: dict[str, _ActiveTrack] = {}

    @property
    def config(self) -> TrackManagerConfig:
        return self._config

    def process(self, detections: list[IncomingDetection]) -> list[Track]:
        """Run one tracking cycle and return the resulting displayable tracks.

        The order below matters. Prediction happens before association so that every track is
        compared against detections at the same instant; associating against stale positions
        would make a fast object look like a mismatch and split its track.
        """
        now = self._clock.now()

        self._predict_all(now)
        result = self._associator.associate(
            [TrackCandidate(t.track_id, t.filter_state) for t in self._active.values()],
            [
                Candidate(d.detection_id, d.position_enu, d.covariance)
                for d in detections
            ],
        )

        by_id = {d.detection_id: d for d in detections}
        for track_id, detection_id in result.assignments.items():
            self._apply_detection(self._active[track_id], by_id[detection_id], now)

        for track_id in result.unassigned_tracks:
            self._coast(self._active[track_id], now)

        for detection_id in result.unassigned_detections:
            self._initiate(by_id[detection_id], now)

        self._retire_expired(now)

        return [self.to_track(t) for t in self._active.values() if t.state.is_displayable]

    def active_tracks(self) -> list[Track]:
        """Every maintained track, including those not yet displayable."""
        return [self.to_track(t) for t in self._active.values()]

    def _predict_all(self, now: datetime) -> None:
        for track in self._active.values():
            dt = (now - track.last_updated).total_seconds()
            if dt > 0.0:
                track.filter_state = self._kalman.predict(track.filter_state, dt)
                track.last_updated = now

    def _apply_detection(
        self, track: _ActiveTrack, detection: IncomingDetection, now: datetime
    ) -> None:
        track.filter_state = self._kalman.update(
            track.filter_state, detection.position_enu, detection.covariance
        )
        track.detection_count += 1
        track.consecutive_misses = 0
        track.last_seen = detection.timestamp
        track.last_updated = now
        track.contributing_sensors.add(detection.sensor_id)

        self._transition_after_detection(track, now)

        self._events.append(
            EventType.TRACK_UPDATED,
            SOURCE,
            payload={
                "detection_id": detection.detection_id,
                "sensor_id": detection.sensor_id,
                "detection_count": track.detection_count,
            },
            track_id=track.track_id,
        )

    def _transition_after_detection(self, track: _ActiveTrack, now: datetime) -> None:
        """Promote on support, and recover a track that was coasting.

        A STALE track that is seen again returns to CONFIRMED rather than starting over. It
        already has the detection history that earned confirmation, and forcing it back through
        promotion would show the operator a "new" object that is not new (FM-022).
        """
        previous = track.state

        if track.state is TrackState.NEW:
            track.state = TrackState.TENTATIVE
        if (
            track.state in (TrackState.TENTATIVE, TrackState.STALE)
            and track.detection_count >= self._config.confirm_after_detections
        ):
            track.state = TrackState.CONFIRMED

        if track.state is not previous:
            self._emit_transition(track, previous, now)

    def _coast(self, track: _ActiveTrack, now: datetime) -> None:
        """Carry a track forward on prediction alone for this cycle.

        Demotion is driven by *elapsed time*, and the track is walked through every state that
        time has earned rather than advancing one step per cycle. This matters: with one step
        per cycle, how long a track survives would depend on how often `process` happened to be
        called, so `coast_seconds` and `stale_seconds` would not mean the durations they name.
        A slow cycle rate would silently keep dead tracks on the operator's screen.

        Each intermediate transition is still emitted separately, so the log shows the track
        passing through STALE on its way to LOST even when one cycle spans both.
        """
        track.consecutive_misses += 1
        elapsed = track.seconds_since_detection(now)
        target = self._demotion_target(track, elapsed)

        while track.state is not target:
            previous = track.state
            track.state = self._next_state_towards(previous, target)
            self._emit_transition(track, previous, now)

        if track.state.is_active:
            self._events.append(
                EventType.TRACK_COASTING,
                SOURCE,
                payload={
                    "consecutive_misses": track.consecutive_misses,
                    "seconds_since_detection": round(elapsed, 3),
                },
                track_id=track.track_id,
            )

    def _demotion_target(self, track: _ActiveTrack, elapsed: float) -> TrackState:
        """The state this track's absence has earned, regardless of cycle timing."""
        if track.state in (TrackState.NEW, TrackState.TENTATIVE):
            # Unsupported tentative tracks are retired quickly: this is the main defence
            # against clutter being promoted into something an operator has to dismiss.
            # Counted in missed cycles rather than seconds, because what disqualifies a
            # tentative track is a lack of corroboration, not the passage of time.
            if track.consecutive_misses >= self._config.tentative_max_misses:
                return TrackState.LOST
            return track.state

        if elapsed > self._config.coast_seconds + self._config.stale_seconds:
            return TrackState.LOST
        if elapsed > self._config.coast_seconds:
            return TrackState.STALE
        return track.state

    @staticmethod
    def _next_state_towards(current: TrackState, target: TrackState) -> TrackState:
        """One step along the lifecycle towards `target`.

        Stepping rather than jumping keeps every transition observable in the log, which is
        what lets replay reconstruct exactly how a track ended.
        """
        order = [
            TrackState.NEW,
            TrackState.TENTATIVE,
            TrackState.CONFIRMED,
            TrackState.STALE,
            TrackState.LOST,
        ]
        return order[min(order.index(current) + 1, order.index(target))]

    def _initiate(self, detection: IncomingDetection, now: datetime) -> None:
        """Start a new track from an unassociated detection."""
        if len(self._active) >= self._config.max_active_tracks:
            # Refusing loudly rather than growing without bound. A shed input is a fact worth
            # reconstructing later, so it is recorded rather than dropped (ADR 0008).
            self._events.append(
                EventType.DETECTION_SHED,
                SOURCE,
                payload={
                    "detection_id": detection.detection_id,
                    "sensor_id": detection.sensor_id,
                    "reason": "max_active_tracks_reached",
                    "max_active_tracks": self._config.max_active_tracks,
                },
            )
            return

        track = _ActiveTrack(
            track_id=self._ids.next(),
            state=TrackState.NEW,
            filter_state=self._kalman.initiate(detection.position_enu, detection.covariance),
            first_seen=detection.timestamp,
            last_seen=detection.timestamp,
            last_updated=now,
            detection_count=1,
            contributing_sensors={detection.sensor_id},
        )
        self._active[track.track_id] = track

        self._events.append(
            EventType.TRACK_CREATED,
            SOURCE,
            payload={
                "detection_id": detection.detection_id,
                "sensor_id": detection.sensor_id,
                "state": str(track.state),
            },
            track_id=track.track_id,
        )

    def _retire_expired(self, now: datetime) -> None:
        """Remove LOST tracks from the active set.

        Removed from *active maintenance*, not deleted: the track's entire history is in the
        event log, which is the source of truth (ADR 0006). Dropping it here frees association
        capacity without losing anything.
        """
        for track_id in [t.track_id for t in self._active.values() if t.state is TrackState.LOST]:
            del self._active[track_id]

    def _emit_transition(
        self, track: _ActiveTrack, previous: TrackState, now: datetime
    ) -> None:
        event_type = {
            TrackState.CONFIRMED: EventType.TRACK_CONFIRMED,
            TrackState.STALE: EventType.TRACK_STALE,
            TrackState.LOST: EventType.TRACK_LOST,
        }.get(track.state, EventType.TRACK_UPDATED)

        self._events.append(
            event_type,
            SOURCE,
            payload={
                "previous_state": str(previous),
                "state": str(track.state),
                "detection_count": track.detection_count,
                "seconds_since_detection": round(track.seconds_since_detection(now), 3),
            },
            track_id=track.track_id,
        )

    def to_track(self, active: _ActiveTrack) -> Track:
        """Project internal state into the immutable published Track."""
        position = active.filter_state.position
        velocity = active.filter_state.velocity

        geodetic = self._frame.to_geodetic(
            EnuPoint(float(position[0]), float(position[1]), float(position[2]))
        )

        speed = float(np.linalg.norm(velocity[:2]))
        heading = float(np.degrees(np.arctan2(velocity[0], velocity[1])) % 360.0)

        return Track(
            track_id=active.track_id,
            state=active.state,
            enu=EnuState(
                east_m=float(position[0]),
                north_m=float(position[1]),
                up_m=float(position[2]),
                velocity_east_mps=float(velocity[0]),
                velocity_north_mps=float(velocity[1]),
                velocity_up_mps=float(velocity[2]),
            ),
            position=TrackPosition(
                latitude=geodetic.latitude_deg,
                longitude=geodetic.longitude_deg,
                altitude_m=geodetic.altitude_m,
            ),
            kinematics=TrackKinematics(
                speed_mps=speed,
                heading_deg=heading,
                vertical_rate_mps=float(velocity[2]),
            ),
            tracking=TrackMetadata(
                first_seen=active.first_seen,
                last_seen=active.last_seen,
                detection_count=active.detection_count,
                missed_updates=active.consecutive_misses,
                track_confidence=self._confidence(active),
                contributing_sensors=sorted(active.contributing_sensors),
            ),
            updated_at=active.last_updated,
        )

    def _confidence(self, active: _ActiveTrack) -> float:
        """How much the system believes this track exists.

        Built from support (how many detections) and freshness (how recently seen), and
        deliberately *not* from the filter's positional certainty — a coasting track's
        position estimate can look tidy while the evidence for the object having ever been
        there is thin. Conflating the two is how a system ends up displaying confident tracks
        on data that stopped arriving.
        """
        support = min(1.0, active.detection_count / max(1, self._config.confirm_after_detections))
        penalty = 1.0 / (1.0 + active.consecutive_misses)
        return round(min(1.0, support * penalty), 4)


def covariance_from_uncertainty(uncertainty_m: float) -> np.ndarray:
    """Isotropic fallback for sources reporting only a scalar uncertainty."""
    return isotropic_covariance(uncertainty_m)
