"""SQLite implementations of the repository interfaces.

Rows are stored as their model's JSON with the columns needed for querying denormalised
alongside. Re-validating the JSON on read means a model change that would silently misread
old rows raises instead — the alternative, a column per field, turns every schema change into
a migration and still fails silently when a type drifts.

Note what the schema does *not* have: the immutability triggers the event log carries. Those
would be wrong here. Projection tables exist to be rebuilt, and reference data exists to be
replaced when configuration changes. The log is the thing that must never be edited, and it
lives in a different file for exactly that reason.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.common.clock import Clock
from backend.common.identifiers import IdGenerator
from backend.database.connection import Database, DatabaseError
from backend.database.records import (
    ProjectedTrack,
    ScenarioDefinition,
    ScenarioRecord,
    SensorRecord,
    TrackHistoryEntry,
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
from backend.tracking.models import Track, TrackState
from backend.zones.site import Site

_SCENARIO_PREFIX = "SCN"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    site_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    document   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sensors (
    sensor_id     TEXT PRIMARY KEY,
    site_id       TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    document      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sensors_site ON sensors (site_id, sensor_id);

CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    document    TEXT NOT NULL
);

-- Projection of the event log. Rebuildable from seq 0; never written except by
-- backend/database/projection.py.
CREATE TABLE IF NOT EXISTS tracks (
    track_id         TEXT PRIMARY KEY,
    state            TEXT    NOT NULL,
    last_applied_seq INTEGER NOT NULL,
    updated_at       TEXT    NOT NULL,
    snapshot         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracks_state ON tracks (state, track_id);

-- Also a projection. Retained after LOST, never deleted (spec §5). Keyed by (track, seq) so
-- re-applying an event replaces its own row instead of appending a duplicate.
CREATE TABLE IF NOT EXISTS track_history (
    track_id   TEXT    NOT NULL,
    seq        INTEGER NOT NULL,
    event_type TEXT    NOT NULL,
    timestamp  TEXT    NOT NULL,
    snapshot   TEXT    NOT NULL,
    PRIMARY KEY (track_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_track_history_seq ON track_history (seq);

CREATE TABLE IF NOT EXISTS projection_state (
    projection       TEXT PRIMARY KEY,
    last_applied_seq INTEGER NOT NULL
);
"""


class SqliteSiteRepository(SiteRepository):
    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(self, site: Site) -> None:
        self._db.execute(
            "INSERT INTO sites (site_id, name, updated_at, document) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(site_id) DO UPDATE SET"
            " name = excluded.name, updated_at = excluded.updated_at,"
            " document = excluded.document",
            (site.site_id, site.name, self._db.now().isoformat(), site.model_dump_json()),
        )

    def get(self, site_id: str) -> Site | None:
        row = self._db.query_one("SELECT document FROM sites WHERE site_id = ?", (site_id,))
        return _validate(Site, row["document"], f"site {site_id}") if row else None

    def list_all(self) -> list[Site]:
        rows = self._db.query("SELECT site_id, document FROM sites ORDER BY site_id")
        return [_validate(Site, r["document"], f"site {r['site_id']}") for r in rows]


class SqliteSensorRepository(SensorRepository):
    def __init__(self, db: Database) -> None:
        self._db = db

    def replace_for_site(self, site_id: str, sensors: Sequence[SensorRecord]) -> None:
        mismatched = [s.sensor_id for s in sensors if s.site_id != site_id]
        if mismatched:
            raise RepositoryError(
                f"sensors {mismatched} do not belong to site {site_id!r}; refusing to file "
                "them under it"
            )
        now = self._db.now().isoformat()
        with self._db.transaction():
            self._db.execute("DELETE FROM sensors WHERE site_id = ?", (site_id,))
            for sensor in sensors:
                self._db.execute(
                    "INSERT INTO sensors (sensor_id, site_id, updated_at, document)"
                    " VALUES (?, ?, ?, ?)",
                    (sensor.sensor_id, site_id, now, sensor.model_dump_json()),
                )

    def get(self, sensor_id: str) -> SensorRecord | None:
        row = self._db.query_one(
            "SELECT document FROM sensors WHERE sensor_id = ?", (sensor_id,)
        )
        return _validate(SensorRecord, row["document"], f"sensor {sensor_id}") if row else None

    def list_all(self, site_id: str | None = None) -> list[SensorRecord]:
        if site_id is None:
            rows = self._db.query(
                "SELECT sensor_id, document FROM sensors ORDER BY site_id, sensor_id"
            )
        else:
            rows = self._db.query(
                "SELECT sensor_id, document FROM sensors WHERE site_id = ? ORDER BY sensor_id",
                (site_id,),
            )
        return [_validate(SensorRecord, r["document"], f"sensor {r['sensor_id']}") for r in rows]


class SqliteScenarioRepository(ScenarioRepository):
    """Scenario definitions, with server-assigned identifiers.

    Identifiers are sequential and resume past what is already stored, for the same reason the
    event store restores its identifier sequence on reopen: a reused identifier makes two
    different scenarios indistinguishable in the record.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ids = IdGenerator(_SCENARIO_PREFIX, width=4)
        self._restore_id_sequence()

    def create(self, definition: ScenarioDefinition, created_by: str) -> ScenarioRecord:
        record = ScenarioRecord(
            scenario_id=self._ids.next(),
            name=definition.name,
            description=definition.description,
            seed=definition.seed,
            definition=definition.definition,
            created_at=self._db.now(),
            created_by=created_by,
        )
        self._db.execute(
            "INSERT INTO scenarios (scenario_id, name, created_at, document)"
            " VALUES (?, ?, ?, ?)",
            (
                record.scenario_id,
                record.name,
                record.created_at.isoformat(),
                record.model_dump_json(),
            ),
        )
        return record

    def get(self, scenario_id: str) -> ScenarioRecord | None:
        row = self._db.query_one(
            "SELECT document FROM scenarios WHERE scenario_id = ?", (scenario_id,)
        )
        return (
            _validate(ScenarioRecord, row["document"], f"scenario {scenario_id}") if row else None
        )

    def list_all(self) -> list[ScenarioRecord]:
        rows = self._db.query("SELECT scenario_id, document FROM scenarios ORDER BY scenario_id")
        return [
            _validate(ScenarioRecord, r["document"], f"scenario {r['scenario_id']}") for r in rows
        ]

    def _restore_id_sequence(self) -> None:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM scenarios")
        for _ in range(row["n"] if row else 0):
            self._ids.next()


class SqliteTrackRepository(TrackRepository):
    def __init__(self, db: Database) -> None:
        self._db = db

    def save(self, projected: ProjectedTrack) -> None:
        track = projected.track
        self._db.execute(
            "INSERT INTO tracks (track_id, state, last_applied_seq, updated_at, snapshot)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(track_id) DO UPDATE SET"
            " state = excluded.state, last_applied_seq = excluded.last_applied_seq,"
            " updated_at = excluded.updated_at, snapshot = excluded.snapshot",
            (
                track.track_id,
                str(track.state),
                projected.last_applied_seq,
                track.updated_at.isoformat(),
                track.model_dump_json(),
            ),
        )

    def get(self, track_id: str) -> ProjectedTrack | None:
        row = self._db.query_one(
            "SELECT last_applied_seq, snapshot FROM tracks WHERE track_id = ?", (track_id,)
        )
        return _projected_track(row, track_id) if row else None

    def list_all(
        self,
        states: Collection[TrackState] | None = None,
        limit: int | None = None,
    ) -> list[ProjectedTrack]:
        sql = "SELECT track_id, last_applied_seq, snapshot FROM tracks"
        params: tuple[Any, ...] = ()
        if states is not None:
            wanted = sorted(str(s) for s in states)
            if not wanted:
                return []
            sql += f" WHERE state IN ({','.join('?' * len(wanted))})"
            params += tuple(wanted)
        sql += " ORDER BY track_id"
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        return [_projected_track(row, row["track_id"]) for row in self._db.query(sql, params)]

    def discard_all(self) -> None:
        self._db.execute("DELETE FROM tracks")


class SqliteTrackHistoryRepository(TrackHistoryRepository):
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, entry: TrackHistoryEntry) -> None:
        self._db.execute(
            "INSERT INTO track_history (track_id, seq, event_type, timestamp, snapshot)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(track_id, seq) DO UPDATE SET"
            " event_type = excluded.event_type, timestamp = excluded.timestamp,"
            " snapshot = excluded.snapshot",
            (
                entry.track_id,
                entry.seq,
                str(entry.event_type),
                entry.timestamp.isoformat(),
                entry.track.model_dump_json(),
            ),
        )

    def for_track(self, track_id: str, limit: int | None = None) -> list[TrackHistoryEntry]:
        sql = (
            "SELECT seq, event_type, timestamp, snapshot FROM track_history"
            " WHERE track_id = ? ORDER BY seq"
        )
        params: tuple[Any, ...] = (track_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        return [
            TrackHistoryEntry(
                seq=row["seq"],
                event_type=row["event_type"],
                timestamp=row["timestamp"],
                track=_validate(Track, row["snapshot"], f"track history {track_id}#{row['seq']}"),
            )
            for row in self._db.query(sql, params)
        ]

    def discard_all(self) -> None:
        self._db.execute("DELETE FROM track_history")


class SqliteProjectionStateRepository(ProjectionStateRepository):
    def __init__(self, db: Database) -> None:
        self._db = db

    def last_applied_seq(self, projection: str) -> int:
        row = self._db.query_one(
            "SELECT last_applied_seq FROM projection_state WHERE projection = ?", (projection,)
        )
        return row["last_applied_seq"] if row else 0

    def record_applied(self, projection: str, seq: int) -> None:
        self._db.execute(
            "INSERT INTO projection_state (projection, last_applied_seq) VALUES (?, ?)"
            " ON CONFLICT(projection) DO UPDATE SET last_applied_seq = excluded.last_applied_seq",
            (projection, seq),
        )

    def reset(self, projection: str) -> None:
        self._db.execute("DELETE FROM projection_state WHERE projection = ?", (projection,))


class SqliteRepositories:
    """Every repository over one connection, opened together.

    A single object because they share a connection and therefore a transaction boundary;
    handing callers five independently constructed repositories over five connections would
    make a unit of work spanning two of them impossible to express.

    Callers hold the individual repositories through their abstract types. This class is the
    composition detail, and is the only place SQLite is named.
    """

    def __init__(self, clock: Clock, path: Path | str | None = None) -> None:
        self._db = Database(clock, path)
        self._db.apply_schema(SCHEMA)

        self.is_durable = self._db.is_durable
        self.sites: SiteRepository = SqliteSiteRepository(self._db)
        self.sensors: SensorRepository = SqliteSensorRepository(self._db)
        self.scenarios: ScenarioRepository = SqliteScenarioRepository(self._db)
        self.tracks: TrackRepository = SqliteTrackRepository(self._db)
        self.track_history: TrackHistoryRepository = SqliteTrackHistoryRepository(self._db)
        self.projection_state: ProjectionStateRepository = SqliteProjectionStateRepository(
            self._db
        )

    def close(self) -> None:
        self._db.close()


def _projected_track(row: sqlite3.Row, track_id: str) -> ProjectedTrack:
    return ProjectedTrack(
        track=_validate(Track, row["snapshot"], f"track {track_id}"),
        last_applied_seq=row["last_applied_seq"],
    )


def _validate(model: type, document: str, what: str) -> Any:
    """Re-validate a stored document against its current model.

    Stored JSON that no longer satisfies its model is a real failure, not a row to skip.
    Skipping it would let the API serve a partial answer that looks complete.
    """
    try:
        return model.model_validate_json(document)
    except (ValidationError, json.JSONDecodeError) as exc:
        raise RepositoryError(f"stored {what} does not match its current model: {exc}") from exc


__all__ = [
    "SCHEMA",
    "DatabaseError",
    "SqliteProjectionStateRepository",
    "SqliteRepositories",
    "SqliteScenarioRepository",
    "SqliteSensorRepository",
    "SqliteSiteRepository",
    "SqliteTrackHistoryRepository",
    "SqliteTrackRepository",
]
