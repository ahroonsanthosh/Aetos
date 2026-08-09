"""REST and WebSocket layer, and the log projection underneath it.

The projection tests matter most. ADR 0006 says the event log is the source of truth and
persisted track state is a projection of it — a claim that is only worth anything if the
projection can genuinely be rebuilt from the log, and if rebuilding twice gives the same
answer. That is what makes backend restart recoverable rather than lossy (FM-030).
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.api.app import API_PREFIX, create_app
from backend.api.identity import LocalOperatorIdentityProvider
from backend.api.services import ApiServices
from backend.common.clock import SimClock
from backend.common.geodesy import GeodeticPoint, LocalFrame
from backend.database.projection import TrackProjection
from backend.database.sqlite import SqliteRepositories
from backend.events.store import SqliteEventStore
from backend.tracking.filters.kalman import isotropic_covariance
from backend.tracking.track_manager.manager import IncomingDetection, TrackManager
from backend.zones.geometry import load_site_geometry

EPOCH = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
R = isotropic_covariance(10.0)


@pytest.fixture
def clock() -> SimClock:
    return SimClock(EPOCH)


@pytest.fixture
def events(clock: SimClock) -> SqliteEventStore:
    store = SqliteEventStore(clock)
    yield store
    store.close()


@pytest.fixture
def repositories(clock: SimClock) -> SqliteRepositories:
    repos = SqliteRepositories(clock)
    yield repos
    repos.close()


@pytest.fixture
def projection(events: SqliteEventStore, repositories: SqliteRepositories) -> TrackProjection:
    return TrackProjection(
        store=events,
        tracks=repositories.tracks,
        history=repositories.track_history,
        state=repositories.projection_state,
    )


@pytest.fixture
def frame() -> LocalFrame:
    return LocalFrame(GeodeticPoint(53.35, -6.26, 0.0))


def drive_tracking(
    clock: SimClock, events: SqliteEventStore, frame: LocalFrame, cycles: int = 8
) -> TrackManager:
    """Produce a genuine track history in the log for the projection to consume."""
    manager = TrackManager(clock, events, frame)
    for i in range(cycles):
        manager.process(
            [
                IncomingDetection(
                    detection_id=f"det-{i}",
                    sensor_id="SIM-RADAR-01",
                    timestamp=clock.now(),
                    position_enu=np.array([i * 20.0, 0.0, 100.0]),
                    covariance=R,
                )
            ]
        )
        clock.advance(1.0)
    return manager


# --------------------------------------------------------------------------------------
# The projection is a projection — ADR 0006
# --------------------------------------------------------------------------------------


def test_projection_reproduces_tracks_from_the_log_alone(
    clock: SimClock,
    events: SqliteEventStore,
    repositories: SqliteRepositories,
    projection: TrackProjection,
    frame: LocalFrame,
) -> None:
    """The claim that makes 'the log is the source of truth' more than a slogan."""
    manager = drive_tracking(clock, events, frame)
    expected = {t.track_id for t in manager.active_tracks()}

    projection.catch_up()

    assert expected
    assert {t.track.track_id for t in projection_tracks(repositories)} >= expected


def projection_tracks(repositories: SqliteRepositories):
    """The repository is the projection's output; reading it back is how a restarted process
    would recover state."""
    return repositories.tracks.list_all()


def test_rebuilding_from_scratch_gives_the_same_answer(
    clock: SimClock,
    events: SqliteEventStore,
    repositories: SqliteRepositories,
    projection: TrackProjection,
    frame: LocalFrame,
) -> None:
    """FM-030. A rebuild that disagreed with the incremental result would mean restart
    silently changes what the system believes."""
    drive_tracking(clock, events, frame)
    projection.catch_up()
    incremental = {t.track.track_id: t.track.state for t in projection_tracks(repositories)}

    projection.rebuild()
    rebuilt = {t.track.track_id: t.track.state for t in projection_tracks(repositories)}

    assert incremental == rebuilt


def test_catching_up_twice_changes_nothing(
    clock: SimClock,
    events: SqliteEventStore,
    repositories: SqliteRepositories,
    projection: TrackProjection,
    frame: LocalFrame,
) -> None:
    """Idempotence. Without it, a retried or resumed catch-up would double-apply events."""
    drive_tracking(clock, events, frame)

    projection.catch_up()
    first = {t.track.track_id: t.track.state for t in projection_tracks(repositories)}
    seq_after_first = projection.last_applied_seq

    projection.catch_up()

    assert projection.last_applied_seq == seq_after_first
    assert {t.track.track_id: t.track.state for t in projection_tracks(repositories)} == first


def test_projection_resumes_from_where_it_stopped(
    clock: SimClock, events: SqliteEventStore, projection: TrackProjection, frame: LocalFrame
) -> None:
    """Recording the last applied seq is what makes a rebuild resumable rather than
    all-or-nothing."""
    manager = drive_tracking(clock, events, frame, cycles=4)
    projection.catch_up()
    partway = projection.last_applied_seq

    for i in range(4, 8):
        manager.process(
            [
                IncomingDetection(
                    detection_id=f"det-{i}",
                    sensor_id="SIM-RADAR-01",
                    timestamp=clock.now(),
                    position_enu=np.array([i * 20.0, 0.0, 100.0]),
                    covariance=R,
                )
            ]
        )
        clock.advance(1.0)

    projection.catch_up()

    assert projection.last_applied_seq > partway
    assert projection.last_applied_seq == events.latest_seq()


def test_an_empty_log_projects_to_nothing(
    repositories: SqliteRepositories, projection: TrackProjection
) -> None:
    projection.catch_up()

    assert projection.last_applied_seq == 0
    assert list(projection_tracks(repositories)) == []


# --------------------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------------------


@pytest.fixture
def client(
    clock: SimClock,
    events: SqliteEventStore,
    repositories: SqliteRepositories,
    projection: TrackProjection,
    frame: LocalFrame,
) -> TestClient:
    drive_tracking(clock, events, frame)
    projection.catch_up()

    site = load_site_geometry("fictional_test_range").site
    repositories.sites.upsert(site)

    services = ApiServices(
        clock=clock,
        events=events,
        sites=repositories.sites,
        sensors=repositories.sensors,
        scenarios=repositories.scenarios,
        tracks=repositories.tracks,
        track_history=repositories.track_history,
        projection=projection,
        identity=LocalOperatorIdentityProvider(clock),
    )
    return TestClient(create_app(services))


def test_tracks_endpoint_returns_tracks(client: TestClient) -> None:
    response = client.get(f"{API_PREFIX}/tracks")

    assert response.status_code == 200
    assert isinstance(response.json(), (list, dict))


def test_sites_endpoint_returns_the_configured_site(client: TestClient) -> None:
    response = client.get(f"{API_PREFIX}/sites")

    assert response.status_code == 200
    assert response.json()


def test_unknown_track_is_a_404_not_an_empty_object(client: TestClient) -> None:
    """Returning an empty track for an unknown ID would let the UI display a phantom."""
    response = client.get(f"{API_PREFIX}/tracks/TRK-99999")

    assert response.status_code == 404


def test_openapi_schema_builds(client: TestClient) -> None:
    """Catches route/model mismatches that would otherwise only appear at request time."""
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["paths"]


def test_no_endpoint_acts_on_the_physical_world(client: TestClient) -> None:
    """The scope boundary, checked at the surface that would expose it.

    Aetos stops at a recommendation. No route may suggest otherwise, whatever its
    implementation does.
    """
    paths = " ".join(client.get("/openapi.json").json()["paths"]).lower()

    for forbidden in ("engage", "fire", "neutralis", "neutraliz", "intercept", "deploy"):
        assert forbidden not in paths, f"API exposes a route containing {forbidden!r}"


# --------------------------------------------------------------------------------------
# System status — run mode and component health
# --------------------------------------------------------------------------------------


def test_mode_reports_simulation_as_a_backend_fact(client: TestClient) -> None:
    """The interface must show simulated-versus-real unmissably, so it has to read a fact the
    backend asserts. A frontend constant would let synthetic data be presented as real by
    editing one line."""
    body = client.get(f"{API_PREFIX}/system/mode").json()

    assert body["mode"] == "simulation"
    assert body["simulated"] is True
    assert body["real_adapters_connected"] == 0


def test_health_reports_unbuilt_modules_as_unbuilt(client: TestClient) -> None:
    """A green light nothing computes is worse than no light. Classification, risk and alerts
    do not exist, and the dashboard must say so rather than showing them as healthy."""
    body = client.get(f"{API_PREFIX}/system/health").json()
    components = {c["component"]: c for c in body["components"]}

    for absent in ("fusion", "classification", "risk", "alerts", "system_health"):
        assert components[absent]["state"] == "not_built"
        assert components[absent]["detail"]


def test_health_reports_working_components_as_ok(client: TestClient) -> None:
    body = client.get(f"{API_PREFIX}/system/health").json()
    components = {c["component"]: c for c in body["components"]}

    assert components["event_store"]["state"] == "ok"
    assert components["database"]["state"] == "ok"
    assert components["tracking"]["state"] == "ok"


def test_response_integration_is_permanently_not_connected(client: TestClient) -> None:
    """The scope boundary, visible on the dashboard the operator actually looks at."""
    body = client.get(f"{API_PREFIX}/system/health").json()
    components = {c["component"]: c for c in body["components"]}

    assert components["response_integration"]["state"] == "not_connected"


def test_health_reports_projection_position_against_the_log(client: TestClient) -> None:
    """Projection lag is the honest measure of whether the operator's picture is current."""
    body = client.get(f"{API_PREFIX}/system/health").json()

    assert body["latest_event_seq"] > 0
    assert body["projection_lag"] == max(0, body["latest_event_seq"] - body["projection_seq"])
