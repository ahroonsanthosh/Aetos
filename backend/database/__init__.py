"""Persistence behind repository interfaces.

Two kinds of table live here and the difference matters: reference data (sites, sensors,
scenario definitions) which nothing can rebuild, and projections (tracks, track history) which
are a materialised read of the event log and can always be rebuilt from `seq` 0. See
`backend/database/README.md` and ADR 0006.
"""

from backend.database.connection import Database, DatabaseError
from backend.database.projection import (
    TRACK_PROJECTION,
    TRACK_SNAPSHOT_KEY,
    ProjectionError,
    TrackProjection,
)
from backend.database.records import (
    ProjectedTrack,
    ScenarioDefinition,
    ScenarioRecord,
    SensorRecord,
    TrackHistoryEntry,
    sensor_records_from_site,
)
from backend.database.repositories import (
    ProjectionStateRepository,
    RepositoryError,
    ScenarioRepository,
    SensorRepository,
    SiteRepository,
    TrackHistoryRepository,
    TrackRepository,
)
from backend.database.sqlite import SqliteRepositories

__all__ = [
    "TRACK_PROJECTION",
    "TRACK_SNAPSHOT_KEY",
    "Database",
    "DatabaseError",
    "ProjectedTrack",
    "ProjectionError",
    "ProjectionStateRepository",
    "RepositoryError",
    "ScenarioDefinition",
    "ScenarioRecord",
    "ScenarioRepository",
    "SensorRecord",
    "SensorRepository",
    "SiteRepository",
    "SqliteRepositories",
    "TrackHistoryEntry",
    "TrackHistoryRepository",
    "TrackProjection",
    "TrackRepository",
    "sensor_records_from_site",
]
