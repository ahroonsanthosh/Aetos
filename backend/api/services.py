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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from backend.alerting.models import Alert
from backend.api.identity import IdentityProvider
from backend.auth.service import AuthService
from backend.common.clock import Clock
from backend.common.config import RunMode
from backend.database.projection import TrackProjection
from backend.database.repositories import (
    ScenarioRepository,
    SensorRepository,
    SiteRepository,
    TrackHistoryRepository,
    TrackRepository,
)
from backend.events.store import EventStore
from backend.risk.correlation import SituationAssessment
from sensors.adapters.interface import SensorAdapter


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
    """Resolves the operator an action is attributed to.

    Which implementation is here decides whether this deployment has access control at all.
    `SessionIdentityProvider` checks a credential; `LocalOperatorIdentityProvider` does not and
    reports `requires_credential == False`, which is what lets the startup guard refuse to
    expose it on a network interface (ADR 0016).
    """

    auth: AuthService | None = None
    """Login, logout and account management.

    Optional, and absent in tests that build an API over a bare projection. When it is None the
    auth routes report themselves unavailable rather than pretending to succeed — an
    authentication endpoint that returns 200 without checking anything is worse than one that is
    missing.
    """

    alerts: AlertSource | None = None
    """Where `GET /alerts` reads open alerts from, and where acknowledgements go.

    Optional on the same terms as `situation`, and absent for the same reason: an API built
    over a bare projection in a test has no alerting service behind it. A missing source is
    reported as *alerting not running* rather than as an empty feed — an empty alert list and
    an absent alerting module must never render identically (ADR 0011).

    Note this is the one place the API reaches state it does not read from the log. It is
    still not authoritative: `AlertService` is itself a projection of the log and rebuilds
    from it at startup. Reading it here rather than rescanning the log per request is what
    stops an alert raised ten thousand events ago from falling outside the scan window and
    silently disappearing from the feed.
    """

    situation: SituationSource | None = None
    """Where `GET /situation` reads the cycle-level finding from.

    Optional, and absent in the tests that construct an API over a bare projection. A missing
    source is reported as *not assessed* rather than as an empty finding — "nothing is running
    the correlation pass" and "the pass ran and found nothing" are different statements, and
    the interface must not show them the same way (ADR 0011).
    """

    classification_running: bool = False
    """Whether a classification service is attached to the pipeline feeding this API.

    A plain flag rather than a handle, because unlike risk and alerting there is nothing for
    the API to *read* from classification — its output already arrives on the track. The flag
    exists only so `GET /system/health` can answer from a fact the wiring asserts instead of
    from a constant, which is how that endpoint came to report classification unbuilt for as
    long as it did after the module landed.
    """

    run_mode: RunMode = RunMode.SIMULATION

    sensor_adapters: Callable[[], Sequence[SensorAdapter]] | None = None
    """The adapters currently feeding the pipeline, read live.

    A callable rather than the list itself: the adapter set is replaced when a scenario or a
    live sensor set is attached, and a captured list would go on describing the previous run.
    None for an API assembled without a pipeline, as most tests do."""
    """Which run mode this deployment is configured for.

    Carried here so `GET /system/mode` reports a fact the backend asserts rather than a
    constant the frontend holds. The interface must show simulated-versus-real unmissably,
    and a frontend-side toggle would let synthetic data be presented as real by editing one
    line."""

    def real_adapter_count(self) -> int:
        """How many non-simulated sensor adapters are feeding the pipeline.

        Counted from the adapters actually attached, via `SensorType.is_simulated`, so the
        answer is a fact about what is running rather than a claim about what was configured.
        A source that fabricates its returns reports a simulated `sensor_type` and is not
        counted here however it was launched — which is what stops a bench stub being
        presented to an operator as a real sensor.

        Zero when nothing has been attached, which is also the honest answer for an idle
        system.
        """
        if self.sensor_adapters is None:
            return 0
        return sum(
            1 for adapter in self.sensor_adapters() if not adapter.sensor_type.is_simulated
        )


class AlertSource(Protocol):
    """Anything that can report and advance open alerts — in practice `AlertService`.

    Note what is absent: no method raises or clears an alert. Those are consequences of
    assessment, decided by the pipeline against a policy; exposing them here would let an API
    request manufacture an alert with no risk finding underneath it, which is precisely the
    kind of thing an operator must be able to rule out when reading the log.
    """

    @property
    def active(self) -> list[Alert]: ...

    def get(self, alert_id: str) -> Alert | None: ...

    def acknowledge(self, alert_id: str, operator_id: str, now: datetime) -> Alert | None: ...


class SituationSource(Protocol):
    """Anything that can report the latest cycle-level assessment — in practice `RiskService`.

    A protocol rather than the class, so the API depends on a capability instead of on the risk
    module's internals.
    """

    @property
    def situation(self) -> SituationAssessment: ...
