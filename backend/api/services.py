"""What the API is given, and what it deliberately is not.

The API holds no authoritative state (spec §14). It has no track table of its own, no cached
alert list, no notion of "current" anything that it maintains. Everything it answers with, it
reads: from the event log by `seq`, or from a projection of that log. The only state that
lives in this process on the API's behalf is each WebSocket client's cursor — a number saying
how far that client has been sent, which is a property of the connection rather than of the
world.

Dependencies are handed in at construction. There is no module-level store, no global engine
and no import-time connection, so a test builds an app over an in-memory database in three
lines and two apps can coexist in one process without discovering each other.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.api.identity import IdentityProvider
from backend.common.clock import Clock
from backend.database.projection import TrackProjection
from backend.database.repositories import (
    ScenarioRepository,
    SensorRepository,
    SiteRepository,
    TrackHistoryRepository,
    TrackRepository,
)
from backend.events.store import EventStore


@dataclass(frozen=True)
class ApiServices:
    """Everything the API reads from or attributes actions to.

    Typed against the abstract repository interfaces rather than the SQLite implementations,
    so swapping the storage engine does not reach into this module (ADR 0001).
    """

    clock: Clock
    events: EventStore
    """Read-only from here. The API appends nothing: it is a read surface and an operator
    action surface, and the operator actions that would append events belong to modules that
    do not exist yet."""

    sites: SiteRepository
    sensors: SensorRepository
    scenarios: ScenarioRepository
    tracks: TrackRepository
    track_history: TrackHistoryRepository
    projection: TrackProjection
    identity: IdentityProvider
