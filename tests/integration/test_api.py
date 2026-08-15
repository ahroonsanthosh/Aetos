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

from backend.alerting.policy import load_alert_policy
from backend.alerting.service import AlertService
from backend.api.app import API_PREFIX, create_app
from backend.api.identity import LocalOperatorIdentityProvider
from backend.api.services import ApiServices
from backend.common.clock import SimClock
from backend.common.config import RunMode
from backend.common.geodesy import GeodeticPoint, LocalFrame
from backend.database.projection import TrackProjection
from backend.database.sqlite import SqliteRepositories
from backend.events.store import SqliteEventStore
from backend.risk.correlation import SituationAssessment
from backend.tracking.filters.kalman import isotropic_covariance
from backend.tracking.models import (
    EnuState,
    RiskLevel,
    Track,
    TrackKinematics,
    TrackMetadata,
    TrackPosition,
    TrackState,
    TrackThreat,
)
from backend.tracking.track_manager.manager import IncomingDetection, TrackManager
from backend.zones.geometry import load_site_geometry
from sensors.adapters.interface import SensorHealth, SensorState
from sensors.schemas.detection import SensorType

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
    response = client.get(f"{API_PREFIX}/openapi.json")

    assert response.status_code == 200
    assert response.json()["paths"]


def test_no_endpoint_acts_on_the_physical_world(client: TestClient) -> None:
    """The scope boundary, checked at the surface that would expose it.

    Aetos stops at a recommendation. No route may suggest otherwise, whatever its
    implementation does.
    """
    paths = " ".join(client.get(f"{API_PREFIX}/openapi.json").json()["paths"]).lower()

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
    """A green light nothing computes is worse than no light.

    Fusion and the health watchdog do not exist at all. Classification, risk and alerting do,
    but this fixture builds an API over a bare projection with no pipeline behind it, so from
    this endpoint's point of view they are equally absent — and it must say so rather than
    reporting a module it has no handle to as healthy.
    """
    body = client.get(f"{API_PREFIX}/system/health").json()
    components = {c["component"]: c for c in body["components"]}

    for absent in ("fusion", "classification", "risk", "alerts", "system_health"):
        assert components[absent]["state"] == "not_built"
        assert components[absent]["detail"]


def test_health_reports_attached_modules_as_running(
    clock: SimClock,
    events: SqliteEventStore,
    repositories: SqliteRepositories,
    projection: TrackProjection,
) -> None:
    """The converse, and the case that regressed silently.

    These rows were hardcoded NOT_BUILT and stayed that way for the whole period after risk
    and classification landed — the dashboard asserted "every track scores zero" while the
    track list showed live scores. Pin the reporting to the handles, so the next module to
    land cannot repeat it.
    """
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
        alerts=AlertService(load_alert_policy(), events),
        situation=_SituationStub(),
        classification_running=True,
    )
    client = TestClient(create_app(services))

    body = client.get(f"{API_PREFIX}/system/health").json()
    components = {c["component"]: c for c in body["components"]}

    for present in ("classification", "risk", "alerts"):
        assert components[present]["state"] == "ok", present
        assert "not running" not in components[present]["detail"]

    # Still absent, and must not have been swept along by the same change.
    assert components["fusion"]["state"] == "not_built"
    assert components["system_health"]["state"] == "not_built"


class _SituationStub:
    """Stands in for `RiskService` as a situation source.

    Only the presence of the handle is under test here — the health endpoint reports whether
    risk is attached, not what it currently thinks.
    """

    @property
    def situation(self) -> SituationAssessment:
        return SituationAssessment()


class _SensorHealthStub:
    """Stands in for `Pipeline` as a sensor health source."""

    def __init__(self, *reports: SensorHealth) -> None:
        self._reports = list(reports)

    def sensor_health(self) -> list[SensorHealth]:
        return self._reports


def _health_report(sensor_id: str, state: SensorState, reason: str) -> SensorHealth:
    return SensorHealth(
        sensor_id=sensor_id,
        sensor_type=SensorType.RADAR,
        state=state,
        reason=reason,
    )


def _services_with_sensors(
    clock, events, repositories, projection, source, live_adapters: int = 0
) -> ApiServices:
    return ApiServices(
        clock=clock,
        events=events,
        sites=repositories.sites,
        sensors=repositories.sensors,
        scenarios=repositories.scenarios,
        tracks=repositories.tracks,
        track_history=repositories.track_history,
        projection=projection,
        identity=LocalOperatorIdentityProvider(clock),
        sensor_health=source,
        live_adapter_count=live_adapters,
        run_mode=RunMode.LIVE if live_adapters else RunMode.SIMULATION,
    )


def test_health_reports_no_sensors_as_not_connected(
    clock: SimClock,
    events: SqliteEventStore,
    repositories: SqliteRepositories,
    projection: TrackProjection,
) -> None:
    services = _services_with_sensors(clock, events, repositories, projection, None)
    client = TestClient(create_app(services))

    body = client.get(f"{API_PREFIX}/system/health").json()
    components = {c["component"]: c for c in body["components"]}

    assert components["sensors"]["state"] == "not_connected"


def test_one_failed_sensor_degrades_the_row_and_is_named(
    clock: SimClock,
    events: SqliteEventStore,
    repositories: SqliteRepositories,
    projection: TrackProjection,
) -> None:
    """A site with four sensors and one dead cable must not average out to healthy.

    The operator's question is never "are most of my sensors fine", and an unplugged radar
    reporting nothing looks exactly like a quiet sky unless something says otherwise (FM-010).
    """
    source = _SensorHealthStub(
        _health_report("radar_01", SensorState.OK, "link open"),
        _health_report("radar_02", SensorState.OFFLINE, "could not open /dev/ttyUSB1"),
    )
    services = _services_with_sensors(
        clock, events, repositories, projection, source, live_adapters=2
    )
    client = TestClient(create_app(services))

    body = client.get(f"{API_PREFIX}/system/health").json()
    components = {c["component"]: c for c in body["components"]}

    assert components["sensors"]["state"] == "degraded"
    assert "radar_02" in components["sensors"]["detail"]
    assert "/dev/ttyUSB1" in components["sensors"]["detail"]


def test_mode_reports_live_only_when_real_adapters_are_attached(
    clock: SimClock,
    events: SqliteEventStore,
    repositories: SqliteRepositories,
    projection: TrackProjection,
) -> None:
    """Declaring LIVE does not change the badge. Attaching a sensor does."""
    declared_only = _services_with_sensors(clock, events, repositories, projection, None)
    body = TestClient(create_app(declared_only)).get(f"{API_PREFIX}/system/mode").json()
    assert body["simulated"] is True
    assert body["real_adapters_connected"] == 0

    attached = _services_with_sensors(
        clock,
        events,
        repositories,
        projection,
        _SensorHealthStub(_health_report("radar_01", SensorState.OK, "link open")),
        live_adapters=1,
    )
    body = TestClient(create_app(attached)).get(f"{API_PREFIX}/system/mode").json()
    assert body["simulated"] is False
    assert body["real_adapters_connected"] == 1


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


# --------------------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------------------


@pytest.fixture
def alerting(events: SqliteEventStore) -> AlertService:
    return AlertService(load_alert_policy(), events)


@pytest.fixture
def alerting_client(
    clock: SimClock,
    events: SqliteEventStore,
    repositories: SqliteRepositories,
    projection: TrackProjection,
    alerting: AlertService,
) -> TestClient:
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
        alerts=alerting,
    )
    return TestClient(create_app(services))


def test_alerts_without_an_alerting_service_say_so(client: TestClient) -> None:
    """The field that stops an empty feed reading as an all-clear. Nothing is evaluating
    tracks here, and the response has to be able to say that (ADR 0011)."""
    body = client.get(f"{API_PREFIX}/alerts").json()

    assert body["alerts"] == []
    assert body["assessing"] is False


def test_alerts_with_a_service_report_assessing(alerting_client: TestClient) -> None:
    body = alerting_client.get(f"{API_PREFIX}/alerts").json()

    assert body["assessing"] is True


def test_a_raised_alert_is_served_with_its_reason_codes(
    alerting_client: TestClient, alerting: AlertService
) -> None:
    alerting.review_cycle([_alerting_track()], EPOCH)

    body = alerting_client.get(f"{API_PREFIX}/alerts").json()

    assert body["count"] == 1
    served = body["alerts"][0]
    assert served["track_id"] == "TRK-ALERT-1"
    assert served["codes"] == ["closing_velocity"]
    assert served["reasons"]
    assert served["peak_level"] == "high"


def test_acknowledging_marks_the_alert_without_clearing_it(
    alerting_client: TestClient, alerting: AlertService
) -> None:
    """The operator action this endpoint exists for, and the thing it must not do."""
    alerting.review_cycle([_alerting_track()], EPOCH)
    alert_id = alerting.active[0].alert_id

    response = alerting_client.post(f"{API_PREFIX}/alerts/{alert_id}/acknowledge")

    assert response.status_code == 200
    assert response.json()["state"] == "acknowledged"
    assert alerting_client.get(f"{API_PREFIX}/alerts").json()["count"] == 1


def test_acknowledging_an_unknown_alert_is_a_404(alerting_client: TestClient) -> None:
    response = alerting_client.post(f"{API_PREFIX}/alerts/ALT-99999/acknowledge")

    assert response.status_code == 404


def test_acknowledging_without_alerting_reports_not_implemented(client: TestClient) -> None:
    response = client.post(f"{API_PREFIX}/alerts/ALT-00001/acknowledge")

    assert response.status_code == 501


def _alerting_track() -> Track:
    """A track already carrying a HIGH assessment, as risk would have written it back."""
    return Track(
        track_id="TRK-ALERT-1",
        state=TrackState.CONFIRMED,
        enu=EnuState(east_m=-900.0, north_m=0.0, up_m=110.0, velocity_east_mps=28.0),
        position=TrackPosition(latitude=51.8971, longitude=-8.4697, altitude_m=110.0),
        kinematics=TrackKinematics(speed_mps=28.0, heading_deg=90.0, vertical_rate_mps=0.0),
        threat=TrackThreat(
            risk_score=82,
            risk_level=RiskLevel.HIGH,
            confidence=0.9,
            reasons=["Closing on the site at 28 m/s."],
            codes=["closing_velocity"],
            assessed=True,
        ),
        tracking=TrackMetadata(first_seen=EPOCH, last_seen=EPOCH, detection_count=14),
        updated_at=EPOCH,
    )


def test_health_warns_when_no_authentication_is_configured(client: TestClient) -> None:
    """This fixture uses `LocalOperatorIdentityProvider` — the `--no-auth` deployment mode.

    That provider resolves an operator for every caller, so the unauthenticated liveness branch
    never runs and cannot carry this warning. Without a row in the full listing, a deployment
    with no access control would report as entirely healthy, which is the exact shape of a green
    light that nothing computes.
    """
    body = client.get(f"{API_PREFIX}/system/health").json()
    posture = {c["component"]: c for c in body["components"]}["access_control"]

    assert posture["state"] == "not_connected"
    assert "NO authentication" in posture["detail"]
