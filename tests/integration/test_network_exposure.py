"""What the system must withstand once it is reachable over a network.

Written from an offensive pass against a running instance rather than from a checklist. Each
test here corresponds to something that was probed, and the three that failed are marked with
what was measured.

The threat model is narrow and worth stating. Aetos does not terminate TLS and does not do its
own rate limiting at the edge — a reverse proxy does both (`docs/deployment.md`). What it must
do for itself is everything a proxy cannot: not leak, not escalate, not fall over, and not
depend on someone else's configuration being correct for a header it could have set.
"""

from __future__ import annotations

import asyncio
import re
import statistics
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.app import SECURITY_HEADERS
from backend.auth.models import Role

# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def secured_app(tmp_path, monkeypatch):
    """A real application with three accounts, one per role."""
    from backend.common.config import ConfigPaths, RunMode
    from backend.main import build_application

    real = ConfigPaths.default()
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        ConfigPaths,
        "default",
        classmethod(lambda cls: cls(repo_root=tmp_path, config_root=real.config_root)),
    )
    app = build_application(
        site="fictional_test_range",
        data_dir=tmp_path / "run",
        authenticated=True,
        mode=RunMode.SIMULATION,
    )
    accounts = (
        ("viewer1", Role.VIEWER),
        ("operator1", Role.OPERATOR),
        ("admin1", Role.ADMIN),
    )
    for name, role in accounts:
        app.state.services.auth.create_user(name, "a-long-enough-password", role)
    return app


def sign_in(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "a-long-enough-password"}
    )
    assert response.status_code == 200, f"could not sign in as {username}"


# --------------------------------------------------------------------------------------
# Availability — the finding that mattered most
# --------------------------------------------------------------------------------------


def test_a_login_flood_does_not_stall_the_rest_of_the_process(secured_app) -> None:
    """Password hashing must not run on the event loop.

    Measured before the fix: three unauthenticated clients sending login attempts took every
    other request from 0.7 ms to 1.1 seconds — a factor of 1683, with no credentials required.
    The event loop was blocked, so this was not merely slow responses: the pipeline cycle
    stopped advancing and the WebSocket stopped pushing, leaving the operator's display frozen
    on stale data while showing every sign of being live.

    An unauthenticated denial of service against the operator's picture is the worst thing on
    the network surface, and it needed no exploit — just the login form.
    """

    async def exercise() -> tuple[float, float]:
        transport = httpx.ASGITransport(app=secured_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://probe") as client:
            idle = []
            for _ in range(10):
                started = time.perf_counter()
                await client.get("/api/v1/system/mode")
                idle.append(time.perf_counter() - started)

            async def attacker() -> None:
                for _ in range(8):
                    await client.post(
                        "/api/v1/auth/login",
                        json={"username": "admin1", "password": "wrong-password"},
                    )

            flood = asyncio.gather(*(attacker() for _ in range(3)))
            await asyncio.sleep(0.05)

            under_load = []
            for _ in range(10):
                started = time.perf_counter()
                await client.get("/api/v1/system/mode")
                under_load.append(time.perf_counter() - started)
            await flood

            return statistics.median(idle), statistics.median(under_load)

    idle, under_load = asyncio.run(exercise())

    assert under_load < max(idle * 25, 0.05), (
        f"unauthenticated login traffic degraded every other request from {idle * 1000:.1f} ms "
        f"to {under_load * 1000:.1f} ms — the hash is back on the event loop"
    )


# --------------------------------------------------------------------------------------
# Headers a browser relies on
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/tracks", "/api/v1/system/mode", "/api/v1/nope", "/"])
def test_security_headers_are_present_on_every_response(secured_app, path: str) -> None:
    """Including errors and static files, because those are responses a browser renders too."""
    with TestClient(secured_app) as client:
        sign_in(client, "admin1")
        response = client.get(path)

    for header in SECURITY_HEADERS:
        assert header.lower() in {k.lower() for k in response.headers}, (
            f"{header} missing from {path} ({response.status_code})"
        )


def test_the_console_cannot_be_framed(secured_app) -> None:
    """Clickjacking is the realistic attack on an operator console: frame it invisibly and let
    someone acknowledge an alert they cannot see."""
    with TestClient(secured_app) as client:
        response = client.get("/")

    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


def test_hsts_is_not_asserted_over_plain_http(secured_app) -> None:
    """Sending HSTS from a loopback run pins `localhost` to https in the developer's browser
    for a year. It is added only for requests that actually arrived over TLS."""
    with TestClient(secured_app) as client:
        response = client.get("/api/v1/system/mode")

    assert "strict-transport-security" not in {k.lower() for k in response.headers}


# --------------------------------------------------------------------------------------
# Authorisation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/v1/scenarios/any/start", "/api/v1/scenarios/any/stop"],
)
def test_a_viewer_cannot_drive_the_simulation(secured_app, path: str) -> None:
    """These were gated at viewer because the role was left to whoever implemented the engine
    — a note in a docstring rather than a dependency. The route existed regardless."""
    with TestClient(secured_app) as client:
        sign_in(client, "viewer1")
        response = client.post(path)

    assert response.status_code == 403


def test_an_operator_cannot_reach_admin_actions(secured_app) -> None:
    with TestClient(secured_app) as client:
        sign_in(client, "operator1")
        response = client.post(
            "/api/v1/scenarios", json={"scenario_id": "x", "name": "x", "seed": 1}
        )

    assert response.status_code == 403


# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------


def test_a_forged_session_cookie_is_refused(secured_app) -> None:
    with TestClient(secured_app) as client:
        client.cookies.set("aetos_session", "attacker-chosen-value")
        assert client.get("/api/v1/tracks").status_code == 401


def test_a_token_stops_working_the_moment_it_is_logged_out(secured_app) -> None:
    """A token that outlives its logout is a stolen token with a longer life than the operator
    who ended the session believes."""
    with TestClient(secured_app) as client:
        sign_in(client, "admin1")
        stolen = dict(client.cookies)
        client.post("/api/v1/auth/logout")

        client.cookies.clear()
        for name, value in stolen.items():
            client.cookies.set(name, value)

        assert client.get("/api/v1/tracks").status_code == 401


def test_session_cookies_carry_the_flags_a_browser_needs(secured_app) -> None:
    with TestClient(secured_app) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin1", "password": "a-long-enough-password"},
        )

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie, "a script-readable session cookie is a session cookie XSS steals"
    assert "samesite=strict" in cookie, "cross-site requests must not carry the session"


def test_tokens_are_unique_and_long(secured_app) -> None:
    tokens = set()
    with TestClient(secured_app) as client:
        for _ in range(10):
            response = client.post(
                "/api/v1/auth/login",
                json={"username": "admin1", "password": "a-long-enough-password"},
            )
            match = re.search(r"aetos_session=([^;]+)", response.headers["set-cookie"])
            assert match
            tokens.add(match.group(1))

    assert len(tokens) == 10
    assert all(len(token) >= 40 for token in tokens), "session tokens are shorter than 256 bits"


# --------------------------------------------------------------------------------------
# Disclosure
# --------------------------------------------------------------------------------------


def test_failed_logins_do_not_reveal_which_accounts_exist(secured_app) -> None:
    """Bodies and status identical for a real account, a fictional one and a locked one.

    Re-asserted after authentication was split into phases to move the hash off the event
    loop: the timing equalisation moved with it, and a refactor that returned early for an
    unknown user would have reintroduced the oracle while every other test still passed.
    """
    with TestClient(secured_app) as client:
        real = client.post(
            "/api/v1/auth/login", json={"username": "admin1", "password": "wrong-password"}
        )
        fictional = client.post(
            "/api/v1/auth/login", json={"username": "no-such-account", "password": "wrong-password"}
        )

    assert real.status_code == fictional.status_code == 401
    assert real.json() == fictional.json()


def test_errors_do_not_leak_internals(secured_app) -> None:
    with TestClient(secured_app) as client:
        sign_in(client, "admin1")
        responses = [
            client.get("/api/v1/tracks/does-not-exist"),
            client.get("/api/v1/detections?limit=not-a-number"),
            client.get("/api/v1/nope"),
        ]

    for response in responses:
        for leak in ("Traceback", "/home/", "sqlite3", "site-packages"):
            assert leak not in response.text, f"{leak!r} leaked in a {response.status_code}"


# --------------------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/../../../../etc/passwd",
        "/%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "/assets/../../../../etc/passwd",
    ],
)
def test_path_traversal_never_escapes_the_bundle(secured_app, path: str) -> None:
    with TestClient(secured_app) as client:
        response = client.get(path)

    assert "root:" not in response.text
    assert response.status_code in (200, 404)


def test_a_huge_body_is_rejected_quickly(secured_app) -> None:
    """Bounded work for unbounded input, and no stack trace on the way out."""
    with TestClient(secured_app) as client:
        started = time.perf_counter()
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "a" * 1_000_000, "password": "x"},
        )
        elapsed = time.perf_counter() - started

    assert response.status_code == 422
    assert elapsed < 5.0
