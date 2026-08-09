"""The track projection: current track state, derived from the event log by replay.

This module is where ADR 0006 stops being a principle and becomes a mechanism.

The event log is the source of truth. The `tracks` and `track_history` tables are a
materialised read of it and nothing else. They exist because answering "what tracks are there
right now" by scanning the whole log on every API request does not scale, not because they
know anything the log does not. Everything the projection stores it read from an event, and
every row can be reconstructed by replaying from `seq` 0. That is the property that makes a
backend restart recoverable rather than lossy (FM-030).

Three consequences worth stating, because a naive implementation gets each of them wrong:

**The projection takes no clock.** Every timestamp it stores comes from the event that
produced it. If it stamped its own time, a rebuilt projection would differ from the original
run and the two would stop being comparable — which is the entire value of being able to
rebuild it.

**Catch-up is at-least-once, and application is idempotent.** The checkpoint is written after
the rows, not atomically with them, so a process that dies mid-batch resumes slightly behind
and re-applies a bounded overlap. That is deliberate: requiring an atomic checkpoint would
push a transaction boundary into the repository interface and make a non-SQLite
implementation harder, whereas idempotent application is a property any store can offer.
Idempotency is therefore load-bearing, not a nicety, and it is tested as such.

**An event the projection cannot apply halts it.** A `TRACK_UPDATED` for a track that was
never created means the projection and the log disagree about what happened. Continuing past
it would produce a projection that silently is not a view of the log, which is precisely the
drift ADR 0006 exists to prevent.
"""

from __future__ import annotations

from pydantic import ValidationError

from backend.database.records import ProjectedTrack, TrackHistoryEntry
from backend.database.repositories import (
    ProjectionStateRepository,
    TrackHistoryRepository,
    TrackRepository,
)
from backend.events.store import EventStore
from backend.events.types import Event, EventType
from backend.tracking.models import Track, TrackState

TRACK_PROJECTION = "tracks"
"""Name this projection records its log position under. Others (alerts, sensor health) will
be separate names against the same repository, each with its own independent position."""

TRACK_SNAPSHOT_KEY = "track"
"""Payload key carrying the full `Track` a lifecycle event refers to.

This is the contract between `backend/tracking` and this projection, and it is stated here
because tracking does not exist yet. A lifecycle event carries the track as it stood after the
change. Carrying the whole snapshot rather than a delta means the projection never has to
guess at state it did not see, and a rebuild reads exactly what was recorded rather than
recomputing it — a projection that recomputed would be a second implementation of tracking,
which is the drift this architecture exists to avoid.
"""

DEFAULT_BATCH_SIZE = 500

_TRACK_EVENTS = frozenset(
    {
        EventType.TRACK_CREATED,
        EventType.TRACK_UPDATED,
        EventType.TRACK_CONFIRMED,
        EventType.TRACK_COASTING,
        EventType.TRACK_STALE,
        EventType.TRACK_LOST,
        # Carries a full track snapshot like the lifecycle events above, so a classification
        # is reconstructible from the log rather than being an in-memory-only conclusion
        # (ADR 0012).
        EventType.CLASSIFICATION_CHANGED,
    }
)

_SNAPSHOT_REQUIRED = frozenset({EventType.TRACK_CREATED, EventType.TRACK_UPDATED})
"""Events that must carry a snapshot. There is no prior state to derive a created or updated
track from, so an absent snapshot is a contract violation rather than a gap to fill in."""

_IMPLIED_STATE = {
    EventType.TRACK_CONFIRMED: TrackState.CONFIRMED,
    EventType.TRACK_STALE: TrackState.STALE,
    EventType.TRACK_LOST: TrackState.LOST,
}
"""Lifecycle events whose type alone determines the resulting state, used when the event
carries no snapshot. `TRACK_COASTING` is absent on purpose: coasting is not a `TrackState`, it
is `missed_updates > 0` on the track's metadata, and only a snapshot can report it."""


class ProjectionError(RuntimeError):
    """The projection could not apply an event.

    Halting is the intended response. A projection that skipped what it could not understand
    would still answer API requests, but its answers would no longer be a view of the log.
    """


class TrackProjection:
    """Current track state and track history, maintained by reading the log by `seq`.

    Reading by `seq` rather than being handed tracks by the tracker is the whole point. It
    makes this class, the WebSocket broadcaster and any future replay engine the same read
    path over the same total order, so none of them can drift from the others.
    """

    def __init__(
        self,
        store: EventStore,
        tracks: TrackRepository,
        history: TrackHistoryRepository,
        state: ProjectionStateRepository,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._store = store
        self._tracks = tracks
        self._history = history
        self._state = state
        self._batch_size = batch_size

    @property
    def last_applied_seq(self) -> int:
        """The log position this projection is current to.

        Published rather than internal: an API response that reports it lets a client open the
        event stream at exactly the next sequence number, so the client sees neither a gap nor
        a duplicate across the handover from REST to WebSocket.
        """
        return self._state.last_applied_seq(TRACK_PROJECTION)

    def catch_up(self) -> int:
        """Apply every event after the recorded position. Returns how many were applied.

        Safe to call repeatedly and safe to call after a crash. Applying an event a track has
        already absorbed is a no-op, so the overlap an at-least-once checkpoint produces
        converges rather than corrupting.
        """
        applied = 0
        while True:
            start = self.last_applied_seq + 1
            batch = list(self._store.read_from(start, limit=self._batch_size))
            if not batch:
                return applied

            for event in batch:
                if event.event_type in _TRACK_EVENTS:
                    self._apply(event)
                    applied += 1

            # After the rows, never before. A checkpoint written first would let a crash lose
            # events permanently; written last it can only cost a repeat, which is harmless.
            self._state.record_applied(TRACK_PROJECTION, batch[-1].seq)

            if len(batch) < self._batch_size:
                return applied

    def rebuild(self) -> int:
        """Discard the projection and reconstruct it from `seq` 0. Returns events applied.

        This is the operation that proves the claim in ADR 0006 rather than merely asserting
        it, and it is what a backend does after a restart it cannot account for.
        """
        self._tracks.discard_all()
        self._history.discard_all()
        self._state.reset(TRACK_PROJECTION)
        return self.catch_up()

    def _apply(self, event: Event) -> None:
        if event.track_id is None:
            raise ProjectionError(
                f"{event.event_type} at seq {event.seq} carries no track_id; a track lifecycle "
                "event that does not say which track it concerns cannot be projected"
            )

        existing = self._tracks.get(event.track_id)
        if existing is not None and existing.last_applied_seq >= event.seq:
            # Already absorbed. This is the idempotency guard, and it is what lets catch-up
            # be at-least-once without an older snapshot overwriting a newer one.
            return

        track = self._track_from(event, existing)

        self._tracks.save(ProjectedTrack(track=track, last_applied_seq=event.seq))
        self._history.record(
            TrackHistoryEntry(
                seq=event.seq,
                event_type=event.event_type,
                timestamp=event.timestamp,
                track=track,
            )
        )

    def _track_from(self, event: Event, existing: ProjectedTrack | None) -> Track:
        """The track as it stood after this event.

        Preference order is snapshot, then implied state over the previous track. A snapshot
        is what the log actually recorded; deriving state from the event type is a fallback
        for lifecycle events that carry nothing else, not an equal alternative.
        """
        snapshot = event.payload.get(TRACK_SNAPSHOT_KEY)

        if snapshot is not None:
            try:
                track = Track.model_validate(snapshot)
            except ValidationError as exc:
                raise ProjectionError(
                    f"{event.event_type} at seq {event.seq} carries a {TRACK_SNAPSHOT_KEY!r} "
                    f"payload that is not a valid Track: {exc}"
                ) from exc
            if track.track_id != event.track_id:
                raise ProjectionError(
                    f"seq {event.seq} is recorded against track {event.track_id!r} but its "
                    f"snapshot describes {track.track_id!r}"
                )
            return track

        if event.event_type in _SNAPSHOT_REQUIRED:
            raise ProjectionError(
                f"{event.event_type} at seq {event.seq} carries no {TRACK_SNAPSHOT_KEY!r} "
                "payload, and there is no prior state to derive the track from"
            )

        if existing is None:
            raise ProjectionError(
                f"{event.event_type} at seq {event.seq} concerns track {event.track_id!r}, "
                "which the projection has never seen created; the log and the projection "
                "disagree about what happened"
            )

        implied = _IMPLIED_STATE.get(event.event_type)
        if implied is None:
            raise ProjectionError(
                f"{event.event_type} at seq {event.seq} carries no {TRACK_SNAPSHOT_KEY!r} "
                "payload and its type does not by itself determine a track state"
            )

        return existing.track.model_copy(update={"state": implied, "updated_at": event.timestamp})
