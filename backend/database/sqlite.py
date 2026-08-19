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
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.auth.models import Credential, Session, User
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
    CredentialRepository,
    ProjectionStateRepository,
    RepositoryError,
    ScenarioRepository,
    SensorRepository,
    SessionRepository,
    SiteRepository,
    TrackHistoryRepository,
    TrackRepository,
    UserRepository,
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

-- Accounts. Reference data: not derived from the log and not rebuildable from it. `username`
-- is UNIQUE at the database level rather than merely checked in Python, because two accounts
-- answering to one login name is a question with no safe answer at authentication time.
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    role       TEXT NOT NULL,
    enabled    INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    document   TEXT NOT NULL
);

-- Password verifiers, in their own table so no query that loads an account also loads its
-- secret. ON DELETE CASCADE is declared for correctness even though accounts are never
-- deleted; an orphaned credential is a live verifier for an account nobody can see.
CREATE TABLE IF NOT EXISTS credentials (
    user_id       TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);

-- Live sessions. The one table here whose rows are meant to be deleted: the audit trail of who
-- logged in and out lives in the event log, so removing a session loses nothing of record and
-- keeping it would leave a usable credential alive past its purpose.
--
-- Indexed on token_hash because that is the only way a session is ever looked up — the token
-- itself is never stored.
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    last_seen_at    TEXT NOT NULL,
    absolute_expiry TEXT NOT NULL,
    document        TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions (absolute_expiry);
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
        # Newest-first with a LIMIT, then reversed, so a truncated history keeps its most
        # recent entries rather than its first ones. `ORDER BY seq LIMIT n` returned the oldest
        # n — the beginning of a route rather than where the object has just been.
        if limit is not None:
            sql = (
                "SELECT seq, event_type, timestamp, snapshot FROM track_history"
                " WHERE track_id = ? ORDER BY seq DESC LIMIT ?"
            )
            params: tuple[Any, ...] = (track_id, limit)
        else:
            sql = (
                "SELECT seq, event_type, timestamp, snapshot FROM track_history"
                " WHERE track_id = ? ORDER BY seq"
            )
            params = (track_id,)
        return [
            TrackHistoryEntry(
                seq=row["seq"],
                event_type=row["event_type"],
                timestamp=row["timestamp"],
                track=_validate(Track, row["snapshot"], f"track history {track_id}#{row['seq']}"),
            )
            for row in sorted(self._db.query(sql, params), key=lambda row: row["seq"])
        ]

    def count_for_track(self, track_id: str) -> int:
        row = self._db.query(
            "SELECT COUNT(*) AS n FROM track_history WHERE track_id = ?", (track_id,)
        )
        return int(next(iter(row))["n"])

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


class SqliteUserRepository(UserRepository):
    """Accounts. Note the absence of a delete: see `UserRepository`."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(self, user: User) -> None:
        self._db.execute(
            "INSERT INTO users (user_id, username, role, enabled, updated_at, document)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            " username = excluded.username, role = excluded.role,"
            " enabled = excluded.enabled, updated_at = excluded.updated_at,"
            " document = excluded.document",
            (
                user.user_id,
                user.username,
                str(user.role),
                int(user.enabled),
                user.updated_at.isoformat(),
                user.model_dump_json(),
            ),
        )

    def get(self, user_id: str) -> User | None:
        row = self._db.query_one("SELECT document FROM users WHERE user_id = ?", (user_id,))
        return _validate(User, row["document"], f"user {user_id}") if row else None

    def by_username(self, username: str) -> User | None:
        row = self._db.query_one("SELECT document FROM users WHERE username = ?", (username,))
        return _validate(User, row["document"], f"user {username!r}") if row else None

    def list_all(self) -> list[User]:
        rows = self._db.query("SELECT user_id, document FROM users ORDER BY username")
        return [_validate(User, r["document"], f"user {r['user_id']}") for r in rows]

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0


class SqliteCredentialRepository(CredentialRepository):
    """Password verifiers.

    Deliberately has no `list_all`. There is no legitimate reason to enumerate every hash in the
    system, and an interface that offers it is an interface someone will eventually use.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def set_for_user(self, credential: Credential) -> None:
        self._db.execute(
            "INSERT INTO credentials (user_id, password_hash, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            " password_hash = excluded.password_hash, updated_at = excluded.updated_at",
            (credential.user_id, credential.password_hash, credential.updated_at.isoformat()),
        )

    def get(self, user_id: str) -> Credential | None:
        row = self._db.query_one(
            "SELECT user_id, password_hash, updated_at FROM credentials WHERE user_id = ?",
            (user_id,),
        )
        if row is None:
            return None
        return Credential(
            user_id=row["user_id"],
            password_hash=row["password_hash"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class SqliteSessionRepository(SessionRepository):
    """Live sessions, looked up only ever by token hash."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, session: Session) -> None:
        self._db.execute(
            "INSERT INTO sessions"
            " (session_id, user_id, token_hash, last_seen_at, absolute_expiry, document)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                session.session_id,
                session.user_id,
                session.token_hash,
                session.last_seen_at.isoformat(),
                session.absolute_expiry.isoformat(),
                session.model_dump_json(),
            ),
        )

    def by_token_hash(self, token_hash: str) -> Session | None:
        row = self._db.query_one(
            "SELECT session_id, document FROM sessions WHERE token_hash = ?", (token_hash,)
        )
        return _validate(Session, row["document"], f"session {row['session_id']}") if row else None

    def touch(self, session_id: str, last_seen_at: datetime) -> None:
        """Slide the idle expiry.

        Rewrites the document as well as the column, because the document is what
        `by_token_hash` re-validates into a `Session` — updating only the column would leave
        every read seeing the login-time value, and the idle timeout would then fire on schedule
        regardless of activity.
        """
        with self._db.transaction():
            row = self._db.query_one(
                "SELECT document FROM sessions WHERE session_id = ?", (session_id,)
            )
            if row is None:
                return
            session = _validate(Session, row["document"], f"session {session_id}")
            renewed = session.model_copy(update={"last_seen_at": last_seen_at})
            self._db.execute(
                "UPDATE sessions SET last_seen_at = ?, document = ? WHERE session_id = ?",
                (last_seen_at.isoformat(), renewed.model_dump_json(), session_id),
            )

    def revoke(self, session_id: str) -> None:
        self._db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def revoke_all_for_user(self, user_id: str) -> int:
        cursor = self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    def purge_expired(self, before: datetime) -> int:
        """Remove sessions past their absolute ceiling.

        Only the absolute expiry is used here, never the idle one: idle expiry depends on the
        policy in force, and a policy loosened tomorrow must not find that yesterday's sweep
        already deleted the sessions it would have kept. Idle expiry is enforced on read, which
        is where the policy is known.
        """
        cursor = self._db.execute(
            "DELETE FROM sessions WHERE absolute_expiry <= ?", (before.isoformat(),)
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


class SqliteRepositories:
    """Every repository over one connection, opened together.

    A single object because they share a connection and therefore a transaction boundary;
    handing callers five independently constructed repositories over five connections would
    make a unit of work spanning two of them impossible to express.

    Callers hold the individual repositories through their abstract types. This class is the
    composition detail, and is the only place SQLite is named.
    """

    def __init__(
        self,
        clock: Clock,
        path: Path | str | None = None,
        identity_path: Path | str | None = None,
    ) -> None:
        self._db = Database(clock, path)
        self._db.apply_schema(SCHEMA)

        # Accounts belong to the installation, not to one deployment's history.
        #
        # Track state is partitioned per site and run mode (ADR 0018, FM-053) because mixing
        # two deployments' observations is how synthetic tracks reach a live display. Accounts
        # are the opposite case: the same person signs in to the simulator and to the live
        # system, and making them create an account per mode would be an obstacle with no
        # safety behind it — the first thing anyone would do is reuse one password everywhere.
        #
        # A second connection rather than a second class, because nothing spans both: auth
        # writes accounts here and its audit trail to the event log, and no unit of work
        # touches an account and a track together.
        self._identity = Database(clock, identity_path) if identity_path is not None else self._db
        if self._identity is not self._db:
            self._identity.apply_schema(SCHEMA)

        self.is_durable = self._db.is_durable
        self.sites: SiteRepository = SqliteSiteRepository(self._db)
        self.sensors: SensorRepository = SqliteSensorRepository(self._db)
        self.scenarios: ScenarioRepository = SqliteScenarioRepository(self._db)
        self.tracks: TrackRepository = SqliteTrackRepository(self._db)
        self.track_history: TrackHistoryRepository = SqliteTrackHistoryRepository(self._db)
        self.users: UserRepository = SqliteUserRepository(self._identity)
        self.credentials: CredentialRepository = SqliteCredentialRepository(self._identity)
        self.sessions: SessionRepository = SqliteSessionRepository(self._identity)
        self.projection_state: ProjectionStateRepository = SqliteProjectionStateRepository(
            self._db
        )

    def close(self) -> None:
        if self._identity is not self._db:
            self._identity.close()
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
