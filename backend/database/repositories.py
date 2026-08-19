"""Repository interfaces.

Two kinds of thing are stored here, and the distinction is the most important idea in this
module:

**Reference data** — sites, sensors, scenario definitions, accounts. Configuration and inputs.
Nothing derives them from the event log and nothing can rebuild them if they are lost.

**Credentials and sessions** are a third kind, called out separately because the distinction
above does not cover them. Accounts are reference data. Sessions are neither reference data nor
a projection: they are live authorisation state, derived from nothing, disposable by design,
and the only rows in the system that *should* be deleted. They get the one bulk-delete method
in these interfaces (`SessionRepository.revoke_all_for_user`), because revoking access has to
be a single operation that cannot half-succeed.

**Projections** — tracks and track history. Derived state, and *only* derived state. The event
log is the source of truth (ADR 0006); these tables are a materialised read of it, kept so the
API can answer "what tracks exist" without scanning the whole log per request. They can be
discarded and rebuilt from `seq` 0 at any time, which is what makes backend restart
recoverable rather than lossy (FM-030).

The interfaces make that distinction visible. Projection repositories expose
`discard_all()` — a legitimate operation on derived data — and reference repositories do not.
Neither exposes a per-row delete: track history is retained after a track goes LOST and is
never deleted (spec §5), so there is no method with which to delete it.

SQLite is the only implementation today. It sits behind these interfaces so PostgreSQL is a
swap rather than a rewrite (ADR 0001).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from backend.database.records import (
    ProjectedTrack,
    ScenarioDefinition,
    ScenarioRecord,
    SensorRecord,
    TrackHistoryEntry,
)
from backend.tracking.models import TrackState
from backend.zones.site import Site

if TYPE_CHECKING:
    # Type-only, and it has to stay that way. `backend.auth.service` depends on these
    # interfaces, so a runtime import here would close the loop
    # auth -> database.repositories -> auth and fail at import time. The annotations are
    # strings under `from __future__ import annotations`, so nothing is needed at runtime.
    from backend.auth.models import Credential, Session, User


class RepositoryError(RuntimeError):
    """A repository operation failed, or stored data no longer matches its model.

    The second case matters more than it looks. Rows are stored as their model's JSON and
    re-validated on read, so a schema change that would silently misread old rows surfaces
    here as an error instead.
    """


class SiteRepository(ABC):
    """Site definitions.

    Reference data loaded from `config/sites/`. No loader exists yet — configuration loading
    belongs to `backend/common` (ADR 0009) and has not been built — so in practice whatever
    composes the system populates this, and the API is a read surface over it.
    """

    @abstractmethod
    def upsert(self, site: Site) -> None:
        """Store a site definition, replacing any existing one with the same `site_id`."""

    @abstractmethod
    def get(self, site_id: str) -> Site | None:
        ...

    @abstractmethod
    def list_all(self) -> list[Site]:
        ...


class SensorRepository(ABC):
    """Sensor placements, derived from site configuration."""

    @abstractmethod
    def replace_for_site(self, site_id: str, sensors: Sequence[SensorRecord]) -> None:
        """Replace the sensor set for one site.

        Wholesale replacement rather than merge: a sensor removed from a site's configuration
        must disappear from the API's answer, not persist as a phantom the operator sees
        listed but never hears from.
        """

    @abstractmethod
    def get(self, sensor_id: str) -> SensorRecord | None:
        ...

    @abstractmethod
    def list_all(self, site_id: str | None = None) -> list[SensorRecord]:
        ...


class ScenarioRepository(ABC):
    """Stored scenario definitions.

    Storing a definition is not running one. Nothing in this repository starts, stops or
    tracks the execution of a scenario, because the scenario engine does not exist.
    """

    @abstractmethod
    def create(self, definition: ScenarioDefinition, created_by: str) -> ScenarioRecord:
        """Store a definition, assigning its identifier and provenance server-side.

        `created_by` comes from the caller's `OperatorContext`, never from the request body.
        """

    @abstractmethod
    def get(self, scenario_id: str) -> ScenarioRecord | None:
        ...

    @abstractmethod
    def list_all(self) -> list[ScenarioRecord]:
        ...


class TrackRepository(ABC):
    """Current track state. **A projection of the event log, not an authority.**

    Nothing writes here except the projection in `backend/database/projection.py`. A module
    that discovers something about a track writes an event; the projection follows. If a
    writer ever appears that does not go through the log, this table stops being a view of
    what happened and becomes a second, disagreeing account of it.
    """

    @abstractmethod
    def save(self, projected: ProjectedTrack) -> None:
        """Insert or replace the row for this track."""

    @abstractmethod
    def get(self, track_id: str) -> ProjectedTrack | None:
        ...

    @abstractmethod
    def list_all(
        self,
        states: Collection[TrackState] | None = None,
        limit: int | None = None,
    ) -> list[ProjectedTrack]:
        """Projected tracks, ordered by identifier. `states` of None means every state,
        including LOST."""

    @abstractmethod
    def discard_all(self) -> None:
        """Empty the projection ahead of a rebuild.

        Legitimate only because this is derived data. The equivalent operation does not exist
        on the event log and never will.
        """


class TrackHistoryRepository(ABC):
    """Every recorded state of every track. Also a projection.

    Retained after a track goes LOST, permanently (spec §5). There is deliberately no method
    to delete one track's history: an object lost and later reacquired is the reason history
    exists, and a per-track delete is how that history gets thrown away by accident.
    """

    @abstractmethod
    def record(self, entry: TrackHistoryEntry) -> None:
        """Store one history point. Keyed by (track, seq), so re-applying an event replaces
        the same row rather than appending a duplicate."""

    @abstractmethod
    def for_track(self, track_id: str, limit: int | None = None) -> list[TrackHistoryEntry]:
        """One track's history in sequence order, oldest first.

        `limit` keeps the **most recent** entries, not the first ones. Where a track has been,
        lately, is the operationally useful half of a long history; returning the oldest N of a
        fifty-thousand-point history would hand an operator a stale partial route with nothing
        saying it was partial. The result is still ordered oldest first.
        """

    @abstractmethod
    def count_for_track(self, track_id: str) -> int:
        """How many entries that track actually has, regardless of any limit.

        Exposed so a truncated response can say it was truncated. A short list that looks
        complete is the failure this whole system is written against."""

    @abstractmethod
    def discard_all(self) -> None:
        """Empty the history projection ahead of a rebuild. See `TrackRepository`."""


class ProjectionStateRepository(ABC):
    """How far each projection has read the log.

    Persisted rather than held in memory, because it is what makes catch-up resumable: a
    backend that restarts reads on from where it stopped instead of replaying the whole log,
    and a backend that crashed mid-batch re-applies a bounded overlap that is idempotent by
    construction.
    """

    @abstractmethod
    def last_applied_seq(self, projection: str) -> int:
        """Highest sequence number applied, or 0 if the projection has read nothing."""

    @abstractmethod
    def record_applied(self, projection: str, seq: int) -> None:
        ...

    @abstractmethod
    def reset(self, projection: str) -> None:
        """Forget the position, so the next catch-up starts from `seq` 0."""


class UserRepository(ABC):
    """Accounts.

    Reference data: nothing derives an account from the log, and nothing can rebuild one if the
    table is lost. So there is no `discard_all()` here, and no per-row delete either — an
    account named in ten thousand `OPERATOR_ACKNOWLEDGED` events cannot be deleted without
    orphaning every one of them. `set_enabled` ends someone's access while leaving the record
    their history points at intact.

    Note what this interface does *not* expose: any way to read a password. Credentials live
    behind `CredentialRepository`, and the separation is deliberate — see
    `backend/auth/models.py`.
    """

    @abstractmethod
    def upsert(self, user: User) -> None:
        """Store an account, replacing any existing one with the same `user_id`."""

    @abstractmethod
    def get(self, user_id: str) -> User | None:
        ...

    @abstractmethod
    def by_username(self, username: str) -> User | None:
        """Look an account up by the name someone types at a login prompt."""

    @abstractmethod
    def list_all(self) -> list[User]:
        ...

    @abstractmethod
    def count(self) -> int:
        """How many accounts exist.

        Its own method rather than `len(list_all())` because the startup guard calls it on every
        boot to decide whether the system has any way to be logged into, and that check must not
        require loading and validating every account to answer.
        """


class CredentialRepository(ABC):
    """Password verifiers, one per account.

    Separate from `UserRepository` so that no code path which loads an account also loads its
    secret. A repository returning both would make leaking the hash a matter of one careless
    response model, rather than something that cannot happen.
    """

    @abstractmethod
    def set_for_user(self, credential: Credential) -> None:
        """Store or replace an account's verifier."""

    @abstractmethod
    def get(self, user_id: str) -> Credential | None:
        ...


class SessionRepository(ABC):
    """Live sessions.

    The one place in this module where deletion is correct. A session is disposable state with
    no historical value — the *fact* that someone logged in and out is recorded in the event
    log, which is where the audit trail lives, so removing the row loses nothing that matters
    and leaving it would keep a usable credential alive past its purpose.

    Sessions are looked up by a hash of the token, never by the token. See
    `backend/auth/models.py` for why.
    """

    @abstractmethod
    def create(self, session: Session) -> None:
        ...

    @abstractmethod
    def by_token_hash(self, token_hash: str) -> Session | None:
        ...

    @abstractmethod
    def touch(self, session_id: str, last_seen_at: datetime) -> None:
        """Record activity, sliding the idle expiry out."""

    @abstractmethod
    def revoke(self, session_id: str) -> None:
        ...

    @abstractmethod
    def revoke_all_for_user(self, user_id: str) -> int:
        """End every session an account holds. Returns how many were ended.

        One operation rather than a loop over `revoke`, because disabling an account or changing
        its role has to take effect everywhere at once. A partial revocation leaves the holder of
        the surviving session with access they were just judged not to have.
        """

    @abstractmethod
    def purge_expired(self, before: datetime) -> int:
        """Delete sessions that can no longer be valid. Returns how many were removed."""
