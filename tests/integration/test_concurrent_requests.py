"""Concurrent authenticated requests, as a browser actually makes them.

The operator console opens with several requests at once — sites, situation, tracks, health —
and every one of them resolves the session, and resolving a session writes to the identity
database (`touch()` slides the expiry). That database is one SQLite connection with no lock,
which is sound only if exactly one thread ever uses it.

It was not. `current_operator` was a plain `def`, and FastAPI runs those in a worker thread, so
simultaneous requests reached the shared connection from different threads at once: the second
`BEGIN IMMEDIATE` landed inside the first transaction and the request failed with
`cannot start a transaction within a transaction`. Logging in to the real console produced a
500 banner and a closed event stream.

Nothing caught it, because every other test in the suite uses `TestClient`, which sends one
request at a time — the one shape of traffic under which the bug cannot occur. These tests send
them together.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from backend.api.app import API_PREFIX, create_app
from backend.api.identity import SessionIdentityProvider
from backend.api.services import ApiServices
from backend.auth.models import Role
from backend.auth.policy import AuthPolicy
from backend.auth.service import AuthService
from backend.common.clock import SimClock
from backend.database.projection import TrackProjection
from backend.database.sqlite import SqliteRepositories
from backend.events.store import SqliteEventStore
from tests.routes import effective_routes

EPOCH = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
PASSWORD = "a-long-enough-passphrase"

READS = ["sites", "situation", "tracks", "sensors", "alerts", "system/health", "auth/me"]
"""What the console requests on load, and then some."""


@pytest.fixture
def app(tmp_path: Path):
    """The real application over durable, WAL-mode databases — the production posture."""
    clock = SimClock(EPOCH)
    repositories = SqliteRepositories(
        clock, tmp_path / "aetos.db", identity_path=tmp_path / "identity.db"
    )
    events = SqliteEventStore(clock, tmp_path / "events.db")
    auth = AuthService(
        AuthPolicy(scrypt_n=2**12, scrypt_r=8, scrypt_p=1),
        clock,
        repositories.users,
        repositories.credentials,
        repositories.sessions,
        events,
    )
    auth.create_user(username="operator", password=PASSWORD, role=Role.ADMIN)

    services = ApiServices(
        clock=clock,
        events=events,
        sites=repositories.sites,
        sensors=repositories.sensors,
        scenarios=repositories.scenarios,
        tracks=repositories.tracks,
        track_history=repositories.track_history,
        projection=TrackProjection(
            store=events,
            tracks=repositories.tracks,
            history=repositories.track_history,
            state=repositories.projection_state,
        ),
        identity=SessionIdentityProvider(clock, auth),
        auth=auth,
    )
    yield create_app(services)
    repositories.close()
    events.close()


async def test_simultaneous_authenticated_requests_all_succeed(app) -> None:
    """The console's opening burst, many times over. Every request must be answered.

    Before the fix, a burst like this failed some fraction of requests with a 500 on almost
    every run; the count varied with scheduling, which is exactly why a single sequential
    request never revealed it.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            f"{API_PREFIX}/auth/login", json={"username": "operator", "password": PASSWORD}
        )
        assert login.status_code == 200

        requests = [client.get(f"{API_PREFIX}/{path}") for path in READS * 12]
        responses = await asyncio.gather(*requests)

    failed = [(r.request.url.path, r.status_code) for r in responses if r.status_code != 200]
    assert not failed, f"{len(failed)} of {len(responses)} concurrent requests failed: {failed[:5]}"


def test_no_dependency_or_handler_runs_in_a_worker_thread(app) -> None:
    """The structural rule that makes the shared connection safe, checked over every route.

    FastAPI runs a plain `def` handler or dependency in a thread pool. The persistence layer is
    one connection with no lock (`backend/database/connection.py`), sound only while every use
    happens on the event-loop thread. So every handler and every dependency must be
    `async def` — including ones that touch no database today, because the next edit to them
    will not remember this file exists.

    Walks the effective route table rather than a list, so a route added later is covered
    without anyone remembering to add it here.
    """
    offenders: list[str] = []

    def walk(dependant, path: str) -> None:
        call = dependant.call
        if call is not None and not (
            inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call)
        ):
            offenders.append(f"{path}: {call.__module__}.{call.__qualname__}")
        for sub in dependant.dependencies:
            walk(sub, path)

    checked = 0
    for route in effective_routes(app):
        dependant = getattr(route.route, "dependant", None)
        if dependant is None:
            continue
        checked += 1
        walk(dependant, route.path or route.name)

    assert checked >= 20, f"only {checked} routes found — the walk is not seeing the app"
    assert not offenders, "these would run in a worker thread:\n  " + "\n  ".join(
        sorted(set(offenders))
    )
