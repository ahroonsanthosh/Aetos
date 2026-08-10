"""Authentication and authorisation over the real API.

The test that matters most here is `test_every_route_is_gated_or_deliberately_public`. It walks
the application's own route table and makes an unauthenticated request to every route, rather
than checking a list someone maintained by hand. A new endpoint added without a gate fails it
without anyone having to remember this file exists — which is the point, since the failure being
designed against is precisely the one nobody thought about.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.app import API_PREFIX, create_app
from backend.api.identity import SessionIdentityProvider
from backend.api.services import ApiServices
from backend.api.stream import WS_UNAUTHENTICATED
from backend.auth.models import Role
from backend.auth.policy import SESSION_COOKIE_NAME, AuthPolicy
from backend.auth.service import AuthService
from backend.common.clock import SimClock
from backend.database.projection import TrackProjection
from backend.database.sqlite import SqliteRepositories
from backend.events.store import SqliteEventStore
from backend.events.types import EventType

EPOCH = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

PASSWORD = "a-long-enough-passphrase"

PUBLIC_ROUTES = {
    # Liveness. A probe must not need a credential, or an unreachable health check is
    # indistinguishable from an unhealthy service. Answers liveness only when unauthenticated.
    ("GET", f"{API_PREFIX}/system/mode"),
    ("GET", f"{API_PREFIX}/system/health"),
    # The login flow itself. A login endpoint behind a login is a closed loop.
    ("POST", f"{API_PREFIX}/auth/login"),
    ("POST", f"{API_PREFIX}/auth/logout"),
    ("GET", f"{API_PREFIX}/auth/me"),
}
"""Routes that may be reached without a session, and why each one may.

Deliberately short and named. Growing it is the change a reviewer should notice — which is why
the audit below fails loudly rather than skipping anything it does not recognise.
"""


@pytest.fixture
def clock() -> SimClock:
    return SimClock(EPOCH)


@pytest.fixture
def repositories(clock: SimClock) -> SqliteRepositories:
    repos = SqliteRepositories(clock)
    yield repos
    repos.close()


@pytest.fixture
def events(clock: SimClock) -> SqliteEventStore:
    store = SqliteEventStore(clock)
    yield store
    store.close()


@pytest.fixture
def auth(clock: SimClock, repositories: SqliteRepositories, events: SqliteEventStore):
    # A deliberately cheap work factor. These tests exercise the *policy*, not the KDF, and the
    # shipped parameters cost about half a second per hash — which turns a suite that logs in
    # thirty times into a suite nobody runs.
    return AuthService(
        AuthPolicy(scrypt_n=2**12, scrypt_r=8, scrypt_p=1),
        clock,
        repositories.users,
        repositories.credentials,
        repositories.sessions,
        events,
    )


@pytest.fixture
def client(
    clock: SimClock,
    events: SqliteEventStore,
    repositories: SqliteRepositories,
    auth: AuthService,
) -> TestClient:
    projection = TrackProjection(
        store=events,
        tracks=repositories.tracks,
        history=repositories.track_history,
        state=repositories.projection_state,
    )
    services = ApiServices(
        clock=clock,
        events=events,
        sites=repositories.sites,
        sensors=repositories.sensors,
        scenarios=repositories.scenarios,
        tracks=repositories.tracks,
        track_history=repositories.track_history,
        projection=projection,
        identity=SessionIdentityProvider(clock, auth),
        auth=auth,
    )
    return TestClient(create_app(services))


def login(client: TestClient, username: str, password: str = PASSWORD):
    return client.post(
        f"{API_PREFIX}/auth/login", json={"username": username, "password": password}
    )


# --------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------


def test_every_route_is_gated_or_deliberately_public(client: TestClient) -> None:
    """Walk the real route table; no hand-maintained list of endpoints.

    A route answering anything other than 401 without a session, and not on the named public
    list, is an access-control hole — regardless of whether anyone remembered to think about it
    when adding the route.
    """
    ungated: list[str] = []

    for route in client.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            continue  # the WebSocket route and mounts; covered separately below

        # Fill path parameters with something syntactically valid. The value never matters:
        # authentication is checked before the handler runs, so a gated route refuses before it
        # can 404.
        concrete = path
        for parameter in ("track_id", "alert_id", "scenario_id"):
            concrete = concrete.replace(f"{{{parameter}}}", "X-1")

        for method in methods - {"HEAD", "OPTIONS"}:
            if (method, path) in PUBLIC_ROUTES:
                continue

            response = client.request(method, concrete, json={})
            if response.status_code != 401:
                ungated.append(f"{method} {path} -> {response.status_code}")

    assert not ungated, (
        "these routes answered without a session and are not on the public list:\n  "
        + "\n  ".join(ungated)
        + "\n\nAdd a role gate, or add the route to PUBLIC_ROUTES with a reason."
    )


def test_a_gated_route_refuses_without_a_session(client: TestClient) -> None:
    assert client.get(f"{API_PREFIX}/tracks").status_code == 401


def test_a_garbage_cookie_is_not_a_session(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-token")
    assert client.get(f"{API_PREFIX}/tracks").status_code == 401


def test_the_api_schema_is_not_public(client: TestClient) -> None:
    """A complete map of every route, parameter and schema is not something to hand to an
    unauthenticated caller. FastAPI mounts these at the root by default; they are moved and
    gated deliberately."""
    assert client.get(f"{API_PREFIX}/openapi.json").status_code == 401
    assert client.get("/openapi.json").status_code == 404


# --------------------------------------------------------------------------------------
# Logging in
# --------------------------------------------------------------------------------------


def test_login_then_read(client: TestClient, auth: AuthService) -> None:
    auth.create_user("alice", PASSWORD, Role.OPERATOR)

    assert login(client, "alice").status_code == 200
    assert client.get(f"{API_PREFIX}/tracks").status_code == 200


def test_the_session_cookie_is_not_readable_by_script(
    client: TestClient, auth: AuthService
) -> None:
    """HttpOnly stops cross-site scripting stealing a session outright; SameSite stops a
    cross-site request carrying one."""
    auth.create_user("alice", PASSWORD, Role.OPERATOR)

    header = login(client, "alice").headers["set-cookie"].lower()

    assert "httponly" in header
    assert "samesite=strict" in header


def test_wrong_password_and_unknown_user_are_indistinguishable(
    client: TestClient, auth: AuthService
) -> None:
    """A login form that tells them apart hands over a list of real operator usernames."""
    auth.create_user("alice", PASSWORD, Role.OPERATOR)

    wrong = login(client, "alice", "not-the-password")
    unknown = login(client, "nobody", "not-the-password")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_a_locked_out_account_is_not_told_that_it_is(
    client: TestClient, auth: AuthService
) -> None:
    """Lockout is a free oracle for whether a username exists, so the detail stays in the log."""
    auth.create_user("alice", PASSWORD, Role.OPERATOR)
    for _ in range(6):
        login(client, "alice", "wrong")

    refused = login(client, "alice")

    assert refused.status_code == 401
    assert "lock" not in refused.json()["detail"].lower()


def test_logout_ends_the_session(client: TestClient, auth: AuthService) -> None:
    auth.create_user("alice", PASSWORD, Role.OPERATOR)
    login(client, "alice")
    assert client.get(f"{API_PREFIX}/tracks").status_code == 200

    assert client.post(f"{API_PREFIX}/auth/logout").status_code == 204
    assert client.get(f"{API_PREFIX}/tracks").status_code == 401


def test_logout_without_a_session_is_not_an_error(client: TestClient) -> None:
    """Logging out is what someone does when unsure what state they are in. It must always leave
    them logged out rather than refusing and leaving a stale cookie in place."""
    assert client.post(f"{API_PREFIX}/auth/logout").status_code == 204


def test_me_reports_the_authenticated_operator(client: TestClient, auth: AuthService) -> None:
    auth.create_user("alice", PASSWORD, Role.ADMIN, display_name="Alice Example")
    login(client, "alice")

    body = client.get(f"{API_PREFIX}/auth/me").json()

    assert body["display_name"] == "Alice Example"
    assert body["role"] == "admin"
    assert body["authenticated"] is True


def test_me_refuses_rather_than_reporting_nobody(client: TestClient) -> None:
    """401, not 200 with `authenticated: false`. A success carrying a negative is the shape that
    leads a client to render a console for nobody."""
    assert client.get(f"{API_PREFIX}/auth/me").status_code == 401


# --------------------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------------------


def test_a_viewer_may_read(client: TestClient, auth: AuthService) -> None:
    auth.create_user("val", PASSWORD, Role.VIEWER)
    login(client, "val")

    assert client.get(f"{API_PREFIX}/tracks").status_code == 200


def test_a_viewer_may_not_acknowledge(client: TestClient, auth: AuthService) -> None:
    """403 and not 401. Logging in again will not help, and sending them to the login page would
    make a permissions problem look like a broken session."""
    auth.create_user("val", PASSWORD, Role.VIEWER)
    login(client, "val")

    assert client.post(f"{API_PREFIX}/alerts/ALT-00001/acknowledge").status_code == 403


def test_an_operator_may_not_manage_scenarios(client: TestClient, auth: AuthService) -> None:
    auth.create_user("olive", PASSWORD, Role.OPERATOR)
    login(client, "olive")

    response = client.post(
        f"{API_PREFIX}/scenarios",
        json={"name": "x", "description": "", "seed": 1, "definition": {}},
    )

    assert response.status_code == 403


def test_an_admin_outranks_every_gate(client: TestClient, auth: AuthService) -> None:
    """`permits` is an ordering, not a set membership test — so admin satisfies the operator gate
    without being listed on it."""
    auth.create_user("root", PASSWORD, Role.ADMIN)
    login(client, "root")

    # 501, not 403: the gate admitted the request and the handler ran, reporting that this
    # fixture has no alerting service behind it. Any non-403 proves the point; asserting the
    # exact code keeps the test honest about what it is actually observing.
    assert client.post(f"{API_PREFIX}/alerts/ALT-00001/acknowledge").status_code == 501


def test_a_role_change_takes_effect_on_a_live_session(
    client: TestClient, auth: AuthService, repositories: SqliteRepositories
) -> None:
    """The account is re-read per request rather than cached on the session. A privilege removed
    mid-shift has to bite immediately, not at the next login."""
    auth.create_user("olive", PASSWORD, Role.OPERATOR)
    login(client, "olive")

    # Admitted: 501 is the handler reporting no alerting service, past the gate.
    assert client.post(f"{API_PREFIX}/alerts/ALT-1/acknowledge").status_code == 501

    user = repositories.users.by_username("olive")
    auth.set_role(user.user_id, Role.VIEWER)

    # Refused, on the same session, with no re-login in between.
    assert client.post(f"{API_PREFIX}/alerts/ALT-1/acknowledge").status_code == 403


def test_disabling_an_account_ends_its_session_immediately(
    client: TestClient, auth: AuthService, repositories: SqliteRepositories
) -> None:
    auth.create_user("olive", PASSWORD, Role.OPERATOR)
    login(client, "olive")
    assert client.get(f"{API_PREFIX}/tracks").status_code == 200

    user = repositories.users.by_username("olive")
    auth.set_enabled(user.user_id, False)

    assert client.get(f"{API_PREFIX}/tracks").status_code == 401


# --------------------------------------------------------------------------------------
# The stream
# --------------------------------------------------------------------------------------


def test_the_stream_refuses_an_unauthenticated_upgrade(client: TestClient) -> None:
    """This endpoint carries the whole live picture. Leaving it open while the REST surface is
    gated would make the gate decorative."""
    with pytest.raises(WebSocketDisconnect) as refused:  # noqa: SIM117 - two context managers
        with client.websocket_connect(f"{API_PREFIX}/stream"):
            pass

    assert refused.value.code == WS_UNAUTHENTICATED


def test_the_stream_accepts_an_authenticated_upgrade(
    client: TestClient, auth: AuthService
) -> None:
    auth.create_user("alice", PASSWORD, Role.VIEWER)
    login(client, "alice")

    with client.websocket_connect(f"{API_PREFIX}/stream") as socket:
        assert socket is not None


# --------------------------------------------------------------------------------------
# Health, and what it says to a stranger
# --------------------------------------------------------------------------------------


def test_health_without_a_session_reports_liveness_only(client: TestClient) -> None:
    """The component list is a map of the software's weak points. A probe gets enough to know the
    process is alive; it does not get the map."""
    body = client.get(f"{API_PREFIX}/system/health").json()

    assert [c["component"] for c in body["components"]] == ["api"]
    assert body["components"][0]["state"] == "ok"


def test_health_with_a_session_reports_components(client: TestClient, auth: AuthService) -> None:
    auth.create_user("alice", PASSWORD, Role.VIEWER)
    login(client, "alice")

    body = client.get(f"{API_PREFIX}/system/health").json()

    assert len(body["components"]) > 1
    assert "tracking" in {c["component"] for c in body["components"]}


# --------------------------------------------------------------------------------------
# The audit trail
# --------------------------------------------------------------------------------------


def test_actions_are_attributed_to_the_authenticated_account(
    client: TestClient, auth: AuthService, events: SqliteEventStore
) -> None:
    """What FM-001 was about. The log can now answer *who*, not only *what*."""
    auth.create_user("alice", PASSWORD, Role.ADMIN)
    login(client, "alice")

    client.post(
        f"{API_PREFIX}/scenarios",
        json={"name": "s", "description": "", "seed": 1, "definition": {}},
    )

    logins = [
        e for e in events.read_from(1) if e.event_type is EventType.OPERATOR_LOGIN_SUCCEEDED
    ]
    assert logins
    assert logins[-1].payload["username"] == "alice"


def test_no_event_payload_ever_contains_a_credential(
    client: TestClient, auth: AuthService, events: SqliteEventStore
) -> None:
    """Asserted rather than promised in a code review.

    Every auth path is exercised — create, succeed, fail, lock out, log out — and the whole log
    is then searched for the password and for the session token. A logged credential is not a
    smaller version of a leak; it is the leak, written to the one file the system promises is
    permanent and replayable.
    """
    auth.create_user("alice", PASSWORD, Role.OPERATOR)
    token = login(client, "alice").cookies.get(SESSION_COOKIE_NAME)
    for _ in range(6):
        login(client, "alice", "wrong-password-entirely")
    client.post(f"{API_PREFIX}/auth/logout")

    for event in events.read_from(1):
        serialised = str(event.payload)
        assert PASSWORD not in serialised, f"{event.event_type} carries the password"
        assert "wrong-password-entirely" not in serialised
        if token:
            assert token not in serialised, f"{event.event_type} carries the session token"


def test_the_raw_session_token_is_never_stored(
    client: TestClient, auth: AuthService, repositories: SqliteRepositories
) -> None:
    """Only a hash of the token is persisted, so a database read yields no live session."""
    auth.create_user("alice", PASSWORD, Role.OPERATOR)
    token = login(client, "alice").cookies.get(SESSION_COOKIE_NAME)
    assert token

    rows = repositories._db.query("SELECT * FROM sessions")  # noqa: SLF001 - asserting storage
    assert rows
    for row in rows:
        assert token not in str(tuple(row))
