"""Application factory.

Composition happens here and only here. The app is built from injected services rather than
importing a store or opening a connection at module scope, so a test constructs one over an
in-memory database and two can coexist in a process without finding each other.

There is no process entrypoint in this module. Nothing yet decides which site to load, which
scenario to run or where the database lives — configuration loading belongs to
`backend/common` (ADR 0009) and has not been built. Adding a `main()` that guessed those would
be the same mistake as an endpoint returning invented data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api import rest, stream, system
from backend.api.schemas import API_PREFIX, API_VERSION
from backend.api.services import ApiServices
from backend.api.stream import EventBroadcaster

TITLE = "Aetos API"

DESCRIPTION = """
Operator API for the Aetos Counter-UAS command-and-control platform.

**Scope.** Aetos is software only and ends at a recommendation presented to a human. No
endpoint here does anything in the physical world, and none ever will. The most severe output
the system can produce is the recommendation category `RESPONSE_RECOMMENDED`, which is advice.

**There is no authentication and no authorisation.** No login, no roles, no checks. Every
action is attributed to a single configured local operator, and that identity is asserted, not
verified — so the event log does not currently prove *who* did anything. Aetos in this state
must not be exposed to an untrusted network. This is a deliberate decision recorded in ADR
0008 and `docs/failure_modes.md` FM-001, not an oversight to be worked around.

**The backend owns all authoritative state.** No endpoint accepts a client's assertion about
what is true. There is no PUT, PATCH or DELETE anywhere in this API.

**Not implemented yet.** Scenario execution returns 501; nothing can run a scenario in this
build. Alerts read the event log honestly and will be empty until a module produces alert
events. Endpoints report the absence of a capability rather than simulating it.
"""


def create_app(services: ApiServices) -> FastAPI:
    """Build the application over the given services."""
    broadcaster = EventBroadcaster(services.events)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Bring the projection up to date, then start listening for commits.

        Catching up at startup is what makes a backend restart recoverable rather than lossy:
        whatever the log recorded while this process was not running is applied before the
        first request is served (FM-030). The stream is started after, so a client cannot be
        served from a projection that is behind the log it is about to be streamed.
        """
        services.projection.catch_up()
        broadcaster.start()
        try:
            yield
        finally:
            broadcaster.stop()

    app = FastAPI(
        title=TITLE,
        version=API_VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.services = services
    app.state.broadcaster = broadcaster

    app.include_router(rest.router, prefix=API_PREFIX)
    app.include_router(stream.router, prefix=API_PREFIX)
    app.include_router(system.router, prefix=API_PREFIX)
    return app
