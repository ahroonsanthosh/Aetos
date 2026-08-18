"""Records the persistence layer stores that have no domain model elsewhere yet.

Sites (`backend/zones/site.py`) and tracks (`backend/tracking/models.py`) already have domain
models, and this module does not restate them — it stores those models directly. What lives
here is the small set of shapes that only exist because something has to be persisted before
the module that owns the concept has been built:

- `SensorRecord` — the sensor registry does not exist yet, so a sensor is currently no more
  than a placement plus whether it is enabled.
- `ScenarioDefinition` / `ScenarioRecord` — the scenario engine does not exist yet, so a
  scenario is currently a stored definition and nothing runs it.
- `ProjectedTrack` / `TrackHistoryEntry` — projection rows, carrying the `seq` they were
  derived from so that re-application is a no-op rather than a rewrite.

When those modules land, the records here should shrink or move, not accumulate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.events.types import EventType
from backend.tracking.models import Track
from backend.zones.site import Site
from sensors.schemas.detection import SensorType


class SensorRecord(BaseModel):
    """A sensor as the system currently knows it: where it is and whether it is enabled.

    Detection characteristics — range, field of view, update rate, uncertainty, probability of
    detection — are deliberately absent. They belong to the sensor registry, which does not
    exist yet. Copying guesses of them here would make the API report capabilities nothing
    has established.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sensor_id: str = Field(min_length=1, max_length=64)
    site_id: str = Field(min_length=1, max_length=64)
    registry_key: str | None = Field(default=None, min_length=1, max_length=64)
    """Null for a real sensor: it has no simulation profile, and inventing one would put
    fabricated detection characteristics where a reader expects configured ones."""

    hardware_driver: str | None = Field(default=None, min_length=1, max_length=64)
    """Which real driver is fitted here, or null for a simulated placement. Carried so that
    `GET /sensors` can distinguish a sensor that exists physically from one that exists only
    in a scenario — the single most important thing about any row in that table."""

    sensor_type: SensorType | None = None
    """Left null rather than guessed. The registry entry that would state a sensor's type has
    no loader yet, and inventing one would put a fact in the database that nothing asserted."""

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    height_m: float = 0.0
    boresight_deg: float = Field(default=0.0, ge=0.0, lt=360.0)
    enabled: bool = True


class ScenarioDefinition(BaseModel):
    """A scenario as submitted: what to run, not a record of anything having run.

    `seed` is required, not defaulted. An unseeded scenario cannot be reproduced, and a
    scenario that cannot be reproduced cannot be replayed against the run it claims to
    replay (ADR 0008).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    seed: int = Field(ge=0)
    definition: dict[str, Any] = Field(default_factory=dict)
    """The scenario body, stored opaquely. The schema that would validate it belongs to the
    scenario engine; validating it here against a shape nobody has agreed would reject
    definitions the engine will later accept."""


class ScenarioRecord(BaseModel):
    """A stored scenario definition, with the provenance the server assigned to it.

    `scenario_id`, `created_at` and `created_by` are server-assigned in every case. A client
    cannot supply them, because a client asserting who created something is exactly the class
    of frontend-asserted state the backend must not treat as truth (spec §14).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    seed: int = Field(ge=0)
    definition: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime
    created_by: str = Field(min_length=1, max_length=128)
    """Operator identity from `OperatorContext`, never from the request body.

    Authenticated in fact since ADR 0016: this names an account that presented a credential.
    Rows written before then carry the single local operator identity, and the event log
    distinguishes the two eras through `Identity.authenticated`."""


class ProjectedTrack(BaseModel):
    """One row of the track projection: a track, and the log position it was derived from.

    Carrying `last_applied_seq` on the row rather than only in a global checkpoint is what
    makes re-applying an event a genuine no-op: the projection can tell whether this
    particular track has already absorbed a given event, so an at-least-once catch-up after a
    crash converges on the same state rather than rewriting rows with older snapshots.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    track: Track
    last_applied_seq: int = Field(ge=1)


class TrackHistoryEntry(BaseModel):
    """One point in a track's recorded life, keyed by the event that produced it.

    History is retained after a track goes LOST and is never deleted (spec §5). An object that
    was lost and later reacquired is the case this exists for: without retained history the
    operator sees a "new" object that is not new (FM-022).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    event_type: EventType
    timestamp: datetime
    """From the event, never from a clock read at projection time. Stamping projection time
    here would make a rebuilt projection differ from the original run, and the two would stop
    being comparable — which is the entire value of rebuilding."""

    track: Track

    @property
    def track_id(self) -> str:
        return self.track.track_id


def sensor_records_from_site(site: Site) -> list[SensorRecord]:
    """Derive sensor rows from a site definition.

    Sensor placement is part of the site document, so the sensors table is a copy of
    configuration rather than an independent authority on where sensors are. It exists so the
    API can answer `GET /sensors` without every reader parsing site documents, and it is
    replaced wholesale when a site is loaded rather than merged — a sensor removed from the
    site configuration must disappear, not linger.
    """
    return [
        SensorRecord(
            sensor_id=placement.sensor_id,
            site_id=site.site_id,
            registry_key=placement.registry_key,
            hardware_driver=placement.hardware.driver if placement.hardware else None,
            latitude=placement.position.latitude,
            longitude=placement.position.longitude,
            height_m=placement.height_m,
            boresight_deg=placement.boresight_deg,
            enabled=placement.enabled,
        )
        for placement in site.sensors
    ]
