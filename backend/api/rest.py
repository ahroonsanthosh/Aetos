"""REST endpoints (spec §15).

Every handler here reads. Three POST endpoints exist — one stores a scenario definition, two
report that they are not implemented — and nothing else changes anything. There is no PUT, no
PATCH and no DELETE anywhere in this module, which is not an accident of scope: the backend
owns all authoritative state (spec §14), and state that a client can assert is not state the
backend owns.

Where a capability does not exist, the endpoint says so rather than inventing a plausible
answer. `POST /scenarios/{id}/start` returns 501 because no scenario engine exists; it does
not append a `SCENARIO_STARTED` event to make the log look busy. `GET /alerts` returns an
empty list because nothing produces alert events yet, and it says why in its own documentation.
An API that quietly fabricates the shape of a capability it does not have is how a system ends
up demonstrated on data no operator could ever have relied on.

Handlers are `async def` and read from SQLite inline. The repository connection has SQLite's
default thread affinity (see `backend/database/README.md`), so a `def` handler dispatched to a
worker thread would touch the connection from the wrong one. At the V0 ceiling — tens of
tracks, tens of events per second — these reads are far below the cost of a round trip, and
holding the loop for them is not measurable. If that stops being true the fix is a connection
per worker thread or PostgreSQL, both of which the repository interface already permits.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import ValidationError

from backend.api.identity import OperatorContext
from backend.api.schemas import (
    DETECTION_SNAPSHOT_KEY,
    AlertListResponse,
    AlertView,
    DetectionListResponse,
    DetectionView,
    PositionView,
    ScenarioCreateRequest,
    ScenarioListResponse,
    ScenarioView,
    SensorListResponse,
    SensorView,
    SiteListResponse,
    SiteView,
    SituationFindingView,
    SituationView,
    TrackHistoryPointView,
    TrackHistoryResponse,
    TrackListResponse,
    TrackView,
)
from backend.api.services import ApiServices
from backend.database.records import ScenarioDefinition
from backend.events.types import Event, EventType
from backend.tracking.models import TrackState
from sensors.schemas.detection import Detection

router = APIRouter()

DEFAULT_SCAN_LIMIT = 1000
MAX_SCAN_LIMIT = 10_000

ALERT_ID_KEY = "alert_id"
"""Payload key by which an `ALERT_CLEARED` event names the alert it clears. Asserted here
because the alerting module does not exist; see the note on `GET /alerts`."""

_NOT_IMPLEMENTED = status.HTTP_501_NOT_IMPLEMENTED

_SCENARIO_ENGINE_ABSENT = (
    "Scenario execution is not implemented. No scenario engine and no simulator exist in this "
    "build, so there is nothing to start or stop. The definition is stored and can be read "
    "back; it cannot be run."
)


def _services(request: Request) -> ApiServices:
    return request.app.state.services


ServicesDep = Annotated[ApiServices, Depends(_services)]


def _operator(services: ServicesDep) -> OperatorContext:
    """The operator an action is attributed to.

    Resolved from server-side configuration and nothing else. No header, cookie or body field
    reaches this, so a client cannot name itself — see `backend/api/identity.py` and FM-001.
    """
    return services.identity.resolve()


OperatorDep = Annotated[OperatorContext, Depends(_operator)]


# --------------------------------------------------------------------------------------
# Tracks — read from the projection, which is a view of the log (ADR 0006)
# --------------------------------------------------------------------------------------


@router.get("/tracks", response_model=TrackListResponse, tags=["tracks"])
async def list_tracks(
    services: ServicesDep,
    state: Annotated[
        list[TrackState] | None,
        Query(description="Filter by lifecycle state. Repeatable. Default excludes LOST."),
    ] = None,
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
) -> TrackListResponse:
    """Current tracks.

    LOST tracks are excluded by default because they are history rather than the current air
    picture; ask for them explicitly with `?state=lost`. They are never deleted (spec §5).

    The response carries `as_of_seq`, the log position the projection was current to. Opening
    the WebSocket stream at `as_of_seq + 1` hands over from this snapshot to the live stream
    with neither a gap nor a duplicate.
    """
    states = set(state) if state else {s for s in TrackState if s is not TrackState.LOST}
    projected = services.tracks.list_all(states=states, limit=limit)
    return TrackListResponse(
        tracks=[TrackView.from_projection(p) for p in projected],
        count=len(projected),
        as_of_seq=services.projection.last_applied_seq,
    )


@router.get("/situation", response_model=SituationView, tags=["tracks"])
async def get_situation(services: ServicesDep) -> SituationView:
    """What the current set of tracks means together.

    Findings no single track can produce: several contacts converging from separate bearings,
    more objects engaged at once than an operator can reasonably attend to, and — honestly
    reported — the display over-counting one object as several while sensor fusion is not built.

    This never modifies any track's risk score. Per-track scoring is a pure function of one
    track, and letting a cycle-level view adjust it would make every score depend on how many
    other tracks happen to exist.
    """
    source = services.situation
    if source is None:
        # Nothing is running the correlation pass. Reported as unassessed rather than as an
        # empty finding: "no one looked" and "we looked and found nothing" are different
        # statements (ADR 0011).
        return SituationView(
            level="none", findings=[], track_count=0, object_count=0, assessed=False
        )

    situation = source.situation
    return SituationView(
        level=str(situation.level),
        findings=[
            SituationFindingView(
                code=finding.code,
                level=str(finding.level),
                detail=finding.detail,
                track_ids=list(finding.track_ids),
            )
            for finding in situation.findings
        ],
        track_count=situation.track_count,
        object_count=situation.object_count,
        assessed=situation.assessed,
    )


@router.get("/tracks/{track_id}", response_model=TrackView, tags=["tracks"])
async def get_track(services: ServicesDep, track_id: Annotated[str, Path()]) -> TrackView:
    """One track, in any lifecycle state including LOST."""
    projected = services.tracks.get(track_id)
    if projected is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no track {track_id!r}")
    return TrackView.from_projection(projected)


@router.get("/tracks/{track_id}/history", response_model=TrackHistoryResponse, tags=["tracks"])
async def get_track_history(
    services: ServicesDep,
    track_id: Annotated[str, Path()],
    limit: Annotated[int | None, Query(ge=1, le=10_000)] = None,
) -> TrackHistoryResponse:
    """Every recorded state of one track, oldest first.

    Beyond the endpoints listed in spec §15, and included because the history it reads is
    retained permanently and would otherwise be unreachable: a track that has gone LOST keeps
    its history so that an object lost and reacquired is not presented as a new one (FM-022).
    """
    entries = services.track_history.for_track(track_id, limit=limit)
    if not entries and services.tracks.get(track_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no track {track_id!r}")

    return TrackHistoryResponse(
        track_id=track_id,
        points=[
            TrackHistoryPointView(
                seq=entry.seq,
                event_type=entry.event_type,
                timestamp=entry.timestamp,
                state=str(entry.track.state),
                position=PositionView(
                    latitude=entry.track.position.latitude,
                    longitude=entry.track.position.longitude,
                    altitude_m=entry.track.position.altitude_m,
                ),
                speed_mps=entry.track.kinematics.speed_mps,
                heading_deg=entry.track.kinematics.heading_deg,
            )
            for entry in entries
        ],
        count=len(entries),
    )


# --------------------------------------------------------------------------------------
# Detections — debug and replay only
# --------------------------------------------------------------------------------------


@router.get("/detections", response_model=DetectionListResponse, tags=["debug"])
async def list_detections(
    services: ServicesDep,
    from_seq: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=MAX_SCAN_LIMIT)] = DEFAULT_SCAN_LIMIT,
) -> DetectionListResponse:
    """Raw detections from the event log. **Debug and replay surface. Not an operator surface.**

    The operator interface displays tracks and never raw detections (spec §3), and
    `docs/architecture.md` records this endpoint as a debug and replay tool. Do not wire it to
    the map. A detection is a single instantaneous observation with no continuity and no
    identity; a map of detections shows clutter, false alarms and one object as many, which
    trains an operator to distrust the display.

    `limit` bounds the number of log entries **scanned**, not the number of detections
    returned. `next_seq` is where to resume. An empty result therefore means "none in this
    window", not "none exist", and the response says which window it looked at.
    """
    scanned = 0
    next_seq = from_seq
    detections: list[DetectionView] = []

    for event in services.events.read_from(from_seq, limit=limit):
        scanned += 1
        next_seq = event.seq + 1
        if event.event_type is not EventType.DETECTION_RECEIVED:
            continue
        detections.append(DetectionView.from_detection(event.seq, _detection_of(event.seq, event)))

    return DetectionListResponse(
        detections=detections,
        count=len(detections),
        from_seq=from_seq,
        next_seq=next_seq,
        scanned=scanned,
    )


# --------------------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------------------


@router.get("/alerts", response_model=AlertListResponse, tags=["alerts"])
async def list_alerts(
    services: ServicesDep,
    from_seq: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=MAX_SCAN_LIMIT)] = DEFAULT_SCAN_LIMIT,
    include_cleared: Annotated[bool, Query()] = False,
) -> AlertListResponse:
    """Alerts, derived from `ALERT_CREATED` events in the log.

    **This will be empty.** No module in this build produces alert events — alerting is build
    stage 8 — so there is nothing for this to find. The endpoint is a real read of the log
    rather than a placeholder returning invented rows, which is why it is honest to expose it:
    the day something appends `ALERT_CREATED`, this answers with it and nothing here changes.

    Two things are provisional. Alerts have no model yet, so `detail` is the recorded payload
    passed through. And this scans the log per request, which does not scale; when alerting
    lands it should get a projection alongside the track one, for the same reason tracks have
    one.
    """
    cleared_ids: set[str] = set()
    created: list[AlertView] = []
    scanned = 0
    next_seq = from_seq

    for event in services.events.read_from(from_seq, limit=limit):
        scanned += 1
        next_seq = event.seq + 1

        if event.event_type is EventType.ALERT_CLEARED:
            alert_id = event.payload.get(ALERT_ID_KEY)
            if isinstance(alert_id, str):
                cleared_ids.add(alert_id)
        elif event.event_type is EventType.ALERT_CREATED:
            created.append(
                AlertView(
                    alert_id=event.event_id,
                    seq=event.seq,
                    created_at=event.timestamp,
                    track_id=event.track_id,
                    source=event.source,
                    cleared=False,
                    detail=event.payload,
                )
            )

    alerts = [a.model_copy(update={"cleared": a.alert_id in cleared_ids}) for a in created]
    if not include_cleared:
        alerts = [a for a in alerts if not a.cleared]

    return AlertListResponse(
        alerts=alerts,
        count=len(alerts),
        from_seq=from_seq,
        next_seq=next_seq,
        scanned=scanned,
    )


# --------------------------------------------------------------------------------------
# Sensors and sites — configuration, read back
# --------------------------------------------------------------------------------------


@router.get("/sensors", response_model=SensorListResponse, tags=["configuration"])
async def list_sensors(
    services: ServicesDep,
    site_id: Annotated[str | None, Query()] = None,
) -> SensorListResponse:
    """Configured sensors and where they are.

    Placement only. Whether a sensor is currently healthy, degraded or silent is
    `backend/system_health`'s answer and that module does not exist, so no health field is
    reported — a green light nothing computes is worse than no light.
    """
    records = services.sensors.list_all(site_id=site_id)
    return SensorListResponse(
        sensors=[SensorView.from_record(r) for r in records], count=len(records)
    )


@router.get("/sites", response_model=SiteListResponse, tags=["configuration"])
async def list_sites(services: ServicesDep) -> SiteListResponse:
    """Configured sites, with their zones.

    Sites are config-driven and never hardcoded; this reads back what was loaded from
    `config/sites/`. Zone geometry is WGS-84, as everything crossing the API boundary is.
    """
    sites = services.sites.list_all()
    return SiteListResponse(sites=[SiteView.from_site(s) for s in sites], count=len(sites))


# --------------------------------------------------------------------------------------
# Scenarios — definitions can be stored; nothing can run them
# --------------------------------------------------------------------------------------


@router.get("/scenarios", response_model=ScenarioListResponse, tags=["scenarios"])
async def list_scenarios(services: ServicesDep) -> ScenarioListResponse:
    """Stored scenario definitions. Storing is not running — see the start endpoint."""
    records = services.scenarios.list_all()
    return ScenarioListResponse(
        scenarios=[ScenarioView.from_record(r) for r in records], count=len(records)
    )


@router.post(
    "/scenarios",
    response_model=ScenarioView,
    status_code=status.HTTP_201_CREATED,
    tags=["scenarios"],
)
async def create_scenario(
    services: ServicesDep,
    operator: OperatorDep,
    request: ScenarioCreateRequest,
) -> ScenarioView:
    """Store a scenario definition.

    The identifier, the creation time and the operator it is attributed to are assigned by the
    server. A client cannot supply them, and a request that tries is rejected rather than
    silently stripped: the backend owns authoritative state (spec §14), and a client asserting
    who created something is precisely what that rules out.

    Storing a definition does not run it. Nothing in this build can.
    """
    record = services.scenarios.create(
        ScenarioDefinition(
            name=request.name,
            description=request.description,
            seed=request.seed,
            definition=request.definition,
        ),
        created_by=operator.identity.operator_id,
    )
    return ScenarioView.from_record(record)


@router.post("/scenarios/{scenario_id}/start", status_code=_NOT_IMPLEMENTED, tags=["scenarios"])
async def start_scenario(
    services: ServicesDep,
    scenario_id: Annotated[str, Path()],
) -> None:
    """Not implemented. Returns 501.

    It appends no event, because nothing started. A `SCENARIO_STARTED` event written by an
    endpoint that starts nothing would make the log a record of something that did not happen,
    and the log's entire value is that it is not that.

    When a scenario engine exists this becomes an operator action and takes an
    `OperatorContext` the way `create_scenario` already does.
    """
    _require_known_scenario(services, scenario_id)
    raise HTTPException(_NOT_IMPLEMENTED, _SCENARIO_ENGINE_ABSENT)


@router.post("/scenarios/{scenario_id}/stop", status_code=_NOT_IMPLEMENTED, tags=["scenarios"])
async def stop_scenario(
    services: ServicesDep,
    scenario_id: Annotated[str, Path()],
) -> None:
    """Not implemented. Returns 501. See `start_scenario`."""
    _require_known_scenario(services, scenario_id)
    raise HTTPException(_NOT_IMPLEMENTED, _SCENARIO_ENGINE_ABSENT)


def _require_known_scenario(services: ApiServices, scenario_id: str) -> None:
    """404 for a scenario that does not exist, before 501 for one that cannot be run.

    Two different facts, and a client acting on them differently deserves to be able to tell
    them apart.
    """
    if services.scenarios.get(scenario_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no scenario {scenario_id!r}")


def _detection_of(seq: int, event: Event) -> Detection:
    """Read the detection a `DETECTION_RECEIVED` event carries.

    A malformed payload raises rather than being skipped. Silently omitting it would make this
    endpoint answer with a subset while looking complete, and a debug tool that lies about what
    the log contains is worse than one that fails.
    """
    snapshot = event.payload.get(DETECTION_SNAPSHOT_KEY)
    if snapshot is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"event at seq {seq} records a detection but carries no "
            f"{DETECTION_SNAPSHOT_KEY!r} payload",
        )
    try:
        return Detection.model_validate(snapshot)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"event at seq {seq} carries a {DETECTION_SNAPSHOT_KEY!r} payload that is not a "
            f"valid Detection: {exc}",
        ) from exc
