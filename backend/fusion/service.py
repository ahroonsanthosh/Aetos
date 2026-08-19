"""Multi-sensor reconciliation: deciding which tracks are one object.

Sits between tracking and classification (`docs/architecture.md`). Its input is the tracks a
cycle produced; its output is the tracks that should be presented, one per object, with the
rest recorded as supporting observations of the same thing.

## Why this is not fixed in association

Global nearest neighbour assigns at most one detection per track per cycle, and that is
correct: two detections of *different* objects must never collapse into one track, which is
what scenario H exists to prove. Three sensors watching one drone therefore deliver three
detections of it, two go unassigned, and unassigned detections start new tracks. Every stage
behaves exactly as designed and an operator still sees three contacts (FM-023).

Loosening the association gate to absorb the surplus would merge genuinely distinct objects —
a far worse failure, and the specific thing ADR 0005 refuses. So the reconciliation happens
here, downstream, where being wrong costs a display grouping rather than a corrupted estimate.

## What this module deliberately does not do

**It does not re-run association** (`docs/architecture.md`, module map).

**It does not merge track state.** No member's position, velocity or covariance is altered, and
no averaged position is invented. A covariance-weighted mean of two estimates of one object is
statistically better than either, and it is still a position no sensor reported — the rest of
this system refuses to display those, and a fusion module is a bad place to make the first
exception. The primary is *chosen* from the members, so every coordinate on screen is one the
tracker actually produced. Estimate-level fusion is a real future step and belongs behind its
own decision record.

**It does not retire anything.** Members stay tracked, stay in the log, and keep their own
history. They are withheld from the presented set, not deleted — an operator asking "why does
this object show three tracks" is entitled to all three.

## Stability, and why it gets this much attention

Two identities have to hold still across cycles or the display lies about continuity:

- **The object id**, or an operator watching one drone sees it renumbered mid-approach.
- **Which member is primary**, or the track id under the cursor changes for no reason the
  operator can see, and any state keyed by track id — risk hysteresis especially — resets with
  it.

Both are held by remembering the previous cycle's assignment and preferring it whenever it is
still valid, rather than recomputing from scratch. See `_assign_object_id` and
`_choose_primary`.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Protocol

from backend.common.identifiers import IdGenerator
from backend.events.store import EventStore
from backend.events.types import EventType
from backend.fusion.models import FusedObject
from backend.fusion.policy import FusionPolicy
from backend.tracking.models import Track, TrackFusion

SOURCE = "fusion"


def _separation(a: Track, b: Track) -> float:
    """Slant distance between two tracks, in metres."""
    return math.dist(
        (a.enu.east_m, a.enu.north_m, a.enu.up_m),
        (b.enu.east_m, b.enu.north_m, b.enu.up_m),
    )


def _is_finite(track: Track) -> bool:
    """Whether a track's position and velocity are numbers a comparison can mean anything about."""
    return all(
        math.isfinite(value)
        for value in (
            track.enu.east_m,
            track.enu.north_m,
            track.enu.up_m,
            track.enu.velocity_east_mps,
            track.enu.velocity_north_mps,
            track.enu.velocity_up_mps,
        )
    )


_OBJECT_PREFIX = "OBJ"


class FusionSink(Protocol):
    """Where a finished fusion result is written back to.

    Narrowed to the one method rather than typed as `TrackManager`, so this module depends on a
    capability instead of on the tracker.
    """

    def apply_fusion(self, track_id: str, fusion: TrackFusion) -> Track | None:
        """Store the result, returning the updated track, or None if it is gone."""
        ...


class FusionService:
    """Reconciles a cycle's tracks into objects and records what changed."""

    def __init__(
        self,
        policy: FusionPolicy,
        events: EventStore,
        sink: FusionSink,
    ) -> None:
        self._policy = policy
        self._events = events
        self._sink = sink
        self._ids = IdGenerator(_OBJECT_PREFIX, width=5)

        self._object_of: dict[str, str] = {}
        """track_id -> object_id, carried between cycles.

        This is what makes object identity survive membership churn: a group inherits the object
        id its members already carry rather than minting a new one, so a drone does not get
        renumbered because a fourth sensor acquired it."""

        self._primary_of: dict[str, str] = {}
        """object_id -> track_id of the incumbent primary, carried between cycles."""

        self._objects: list[FusedObject] = []

    @property
    def objects(self) -> list[FusedObject]:
        """The most recent cycle's objects, primary-first order preserved."""
        return list(self._objects)

    def fuse_cycle(self, tracks: list[Track]) -> list[Track]:
        """Reconcile one cycle's tracks. Returns the tracks to present downstream.

        The returned list holds one track per object — the primary, carrying the object's roster
        and corroborating sensors. Supporting members are written back too, marked non-primary,
        so the log and the projection carry the whole picture even though only primaries go on
        to classification, risk and alerting.
        """
        groups = self._group(tracks)
        object_ids = self._assign_object_ids(groups)
        objects: list[FusedObject] = []
        presented: list[Track] = []

        seen_object_ids = set(object_ids)

        for group, object_id in zip(groups, object_ids, strict=True):
            primary = self._choose_primary(object_id, group)

            ordered = (primary, *sorted(
                (t for t in group if t.track_id != primary.track_id),
                key=lambda t: t.track_id,
            ))
            sensors = tuple(
                sorted({s for member in ordered for s in member.tracking.contributing_sensors})
            )
            member_ids = [member.track_id for member in ordered]

            stored_primary = self._sink.apply_fusion(
                primary.track_id,
                TrackFusion(
                    assessed=True,
                    object_id=object_id,
                    is_primary=True,
                    member_track_ids=member_ids,
                    corroborating_sensors=list(sensors),
                ),
            )
            for member in ordered[1:]:
                self._sink.apply_fusion(
                    member.track_id,
                    TrackFusion(
                        assessed=True,
                        object_id=object_id,
                        is_primary=False,
                        # The roster lives on the primary only: duplicating it here would make
                        # the same fact correctable in several places, and they would drift.
                        member_track_ids=[],
                        corroborating_sensors=[],
                    ),
                )

            if stored_primary is None:
                # Retired between tracking and fusion. Nothing to present, and nothing to
                # record about a track that no longer exists.
                continue

            objects.append(
                FusedObject(
                    object_id=object_id,
                    primary=stored_primary,
                    members=ordered,
                    corroborating_sensors=sensors,
                )
            )
            presented.append(stored_primary)

        self._record_changes(objects)
        self._forget_absent({t.track_id for t in tracks}, seen_object_ids)
        self._objects = objects
        return presented

    # -----------------------------------------------------------------------------------
    # Grouping
    # -----------------------------------------------------------------------------------

    def _group(self, tracks: list[Track]) -> list[list[Track]]:
        """Tracks partitioned into likely objects.

        Single-link connected components of the "probably the same object" relation, with a
        track bridging two groups merging them. Anything less is order-dependent: with A and C
        acquired first they open two groups, and B arriving later joins whichever it is compared
        against first, leaving the other stranded — so the same three fragments would be one
        object or two depending on the order the sensors happened to acquire them.
        """
        groups: list[list[Track]] = []

        for track in sorted(tracks, key=lambda t: t.track_id):
            matching = [group for group in groups if self._joins(track, group)]
            if not matching:
                groups.append([track])
                continue

            merged = [track, *(member for group in matching for member in group)]
            if len(matching) == 1 or self._within_extent(merged):
                for other in matching:
                    groups.remove(other)
                groups.append(merged)
                continue

            # Bridging several groups would breach the extent ceiling, so the bridge is not
            # taken and the track joins only the nearest of them. Chaining is wanted between
            # fragments of one object and catastrophic across a swarm; this is where the
            # difference is enforced.
            nearest = min(
                matching,
                key=lambda group: min(_separation(track, member) for member in group),
            )
            nearest.append(track)

        return groups

    def _joins(self, track: Track, group: list[Track]) -> bool:
        """Whether a track may join a group.

        Two conditions, and they are deliberately different shapes. It must look like the same
        object as *some* member — single-link, because fragments of one object form a chain that
        requiring every pair to agree would break. And it must sit within the extent ceiling of
        *every* member, which is what stops that chain running away across a swarm.
        """
        if not any(self._same_object(track, member) for member in group):
            return False
        return all(
            _separation(track, member) <= self._policy.max_object_extent_m for member in group
        )

    def _within_extent(self, members: list[Track]) -> bool:
        """Whether every pair in a prospective group sits inside the extent ceiling."""
        return all(
            _separation(a, b) <= self._policy.max_object_extent_m
            for index, a in enumerate(members)
            for b in members[index + 1 :]
        )

    def _same_object(self, a: Track, b: Track) -> bool:
        """Whether two tracks are plausibly observations of one object.

        Both halves must hold. Separation alone would merge a crossing pair at the moment they
        pass, which is the failure scenario H is written to catch.
        """
        if not (self._fusable(a) and self._fusable(b)):
            return False
        if not (_is_finite(a) and _is_finite(b)):
            # A non-finite coordinate fails every comparison rather than passing them. `NaN >
            # limit` is False, so an unguarded separation test *accepts* a NaN position and
            # merges two tracks that could be anywhere — the failure direction that hides a
            # contact. Refusing to fuse is the safe reading of "we do not know where this is".
            return False

        # Wider limits for a pair already fused, so the grouping does not chatter across the
        # threshold. `_object_of` still holds last cycle's assignment at this point, which is
        # exactly the "were these together" question the band needs answered.
        together = self._were_together(a, b)
        separation_limit = (
            self._policy.split_separation_m if together else self._policy.separation_m
        )
        velocity_limit = self._policy.split_velocity_mps if together else self._policy.velocity_mps

        if _separation(a, b) > separation_limit:
            return False

        if self._policy.require_both_moving and (
            a.kinematics.speed_mps < self._policy.stationary_speed_mps
            and b.kinematics.speed_mps < self._policy.stationary_speed_mps
        ):
            # Two hovering objects both have near-zero velocity, so the co-moving test passes
            # trivially and separation alone would decide — the one case where the
            # discriminating half of the test stops discriminating. Two loitering drones a
            # hundred metres apart are two drones.
            return False

        difference = math.dist(
            (a.enu.velocity_east_mps, a.enu.velocity_north_mps, a.enu.velocity_up_mps),
            (b.enu.velocity_east_mps, b.enu.velocity_north_mps, b.enu.velocity_up_mps),
        )
        return difference <= velocity_limit

    def _were_together(self, a: Track, b: Track) -> bool:
        """Whether these two were reconciled to the same object on the previous cycle."""
        held = self._object_of.get(a.track_id)
        return held is not None and held == self._object_of.get(b.track_id)

    def _fusable(self, track: Track) -> bool:
        """Whether a track carries enough support to be reconciled with another at all.

        A track from a single return has a position seeded from one measurement and a velocity
        of zero with enormous uncertainty. Its apparent agreement with a real track is not
        evidence, and admitting it would let clutter attach itself to a real object and inflate
        that object's corroboration.
        """
        return track.tracking.detection_count >= self._policy.minimum_detections

    # -----------------------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------------------

    def _assign_object_ids(self, groups: list[list[Track]]) -> list[str]:
        """One object id per group, resolved across the whole cycle at once.

        Inheriting rather than recomputing is what keeps an object's identity stable while its
        membership changes underneath it — a fourth sensor acquiring the drone must not renumber
        it. But inheritance has to be resolved *globally*, because an id can be claimed by more
        than one group at a time and exactly one of them can have it.

        That is not a corner case. When a group splits — one fragment drifting outside the gate
        for a few cycles, which happens constantly — both halves still carry the old id, so both
        inherit it. Assigning per group let two groups hold the same id, the incumbent-primary
        record was then overwritten by whichever was processed last, and the presented track
        flipped between them every cycle. Measured on `b_approaching_threat`: 320 of 640 cycles
        held a duplicated id and the primary flipped 708 times, and because alerts key on the
        presented track, each flip cleared one alert and raised another. One drone produced 43
        alerts where the unfused pipeline produced 3.

        So a contested id goes to exactly one group, chosen in this order:

        1. **The group holding the incumbent primary.** Identity follows the track the operator
           has been looking at, which is the whole point of keeping it stable.
        2. **The group with most of the object's tracks.** Absent the primary, identity follows
           the majority of the observations rather than an arbitrary fragment.
        3. **The lowest track id**, so the outcome is deterministic under replay (ADR 0008).

        Groups that win nothing mint a fresh id: a fragment that has genuinely separated is a
        new object, and saying so is better than two things sharing a name.
        """
        claims: list[Counter[str]] = [
            Counter(
                self._object_of[track.track_id]
                for track in group
                if track.track_id in self._object_of
            )
            for group in groups
        ]

        contested: dict[str, list[int]] = defaultdict(list)
        for index, claim in enumerate(claims):
            for object_id in claim:
                contested[object_id].append(index)

        assigned: list[str | None] = [None] * len(groups)

        # Sorted so that where a group could inherit several ids — two objects that have
        # converged — it takes the oldest, and the younger id is retired rather than reused.
        for object_id in sorted(contested):
            candidates = [i for i in contested[object_id] if assigned[i] is None]
            if not candidates:
                continue

            incumbent = self._primary_of.get(object_id)
            winner = next(
                (
                    i
                    for i in candidates
                    if incumbent is not None
                    and any(t.track_id == incumbent for t in groups[i])
                ),
                None,
            )
            if winner is None:
                winner = min(
                    candidates,
                    key=lambda i: (
                        -claims[i][object_id],
                        min(t.track_id for t in groups[i]),
                    ),
                )
            assigned[winner] = object_id

        resolved: list[str] = []
        for index, group in enumerate(groups):
            object_id = assigned[index] or self._ids.next()
            resolved.append(object_id)
            for track in group:
                self._object_of[track.track_id] = object_id
        return resolved

    def _choose_primary(self, object_id: str, group: list[Track]) -> Track:
        """Which member stands for the object.

        The incumbent keeps the role while it remains in the group. Recomputing every cycle
        would hand it to whichever track happens to be marginally best supported at that
        instant, and detection counts cross constantly — so the presented track id would flicker
        between fragments of one drone, taking every piece of state keyed by track id with it.
        Risk hysteresis is the one that matters: its dwell timers live per track, and a primary
        that changed would reset the level of an object mid-escalation.

        A new object picks the best-supported member: most detections, then the longest-lived,
        then the lowest track id so the choice is deterministic under replay.
        """
        incumbent_id = self._primary_of.get(object_id)
        incumbent = next((t for t in group if t.track_id == incumbent_id), None)
        if incumbent is not None:
            return incumbent

        chosen = min(
            group,
            key=lambda t: (
                -t.tracking.detection_count,
                t.tracking.first_seen,
                t.track_id,
            ),
        )
        self._primary_of[object_id] = chosen.track_id
        return chosen

    def _forget_absent(self, present_track_ids: set[str], present_objects: set[str]) -> None:
        """Drop remembered identity for tracks and objects that are no longer in the picture."""
        for track_id in [t for t in self._object_of if t not in present_track_ids]:
            del self._object_of[track_id]
        for object_id in [o for o in self._primary_of if o not in present_objects]:
            del self._primary_of[object_id]

    # -----------------------------------------------------------------------------------
    # Recording
    # -----------------------------------------------------------------------------------

    def _record_changes(self, objects: list[FusedObject]) -> None:
        """Record objects whose membership changed since the last cycle.

        Only changes: an event per fused object per cycle would bury the log at sensor rate, and
        the fusion state already travels on every track snapshot. What is worth its own entry is
        the moment the system decided two contacts were one, because that is a claim an operator
        may later need to see the basis for.
        """
        previous = {obj.object_id: obj.member_ids for obj in self._objects}

        for obj in objects:
            if not obj.is_fused:
                continue
            if previous.get(obj.object_id) == obj.member_ids:
                continue

            self._events.append(
                EventType.TRACKS_FUSED,
                SOURCE,
                payload={
                    "object_id": obj.object_id,
                    "primary_track_id": obj.primary.track_id,
                    "member_track_ids": list(obj.member_ids),
                    "corroborating_sensors": list(obj.corroborating_sensors),
                    "previous_member_track_ids": list(previous.get(obj.object_id, ())),
                },
                track_id=obj.primary.track_id,
            )
