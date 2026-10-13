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

from fastapi import Depends, FastAPI, Request, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse

from backend.api import auth_routes, rest, stream, system
from backend.api.deps import require_role
from backend.api.schemas import API_PREFIX, API_VERSION
from backend.api.services import ApiServices
from backend.api.stream import EventBroadcaster
from backend.auth.models import Role

TITLE = "Aetos API"

DESCRIPTION = """
Operator API for the Aetos Counter-UAS command-and-control platform.

**Scope.** Aetos is software only and ends at a recommendation presented to a human. No
endpoint here does anything in the physical world, and none ever will. The most severe output
the system can produce is the recommendation category `RESPONSE_RECOMMENDED`, which is advice.

**Authentication is required for everything except a liveness probe.** Log in at
`POST /auth/login` for a session cookie; every other route needs it. Roles are `viewer` (read),
`operator` (read and acknowledge alerts) and `admin` (also manage accounts). A request above the
caller's role is refused with 403 rather than 401, because logging in again would not help.

A deployment *can* still run without authentication, for a single-user local machine. When it
does, `GET /system/health` says so in as many words and the backend refuses to bind to anything
but loopback. See ADR 0016.

**The backend owns all authoritative state.** No endpoint accepts a client's assertion about
what is true. There is no PUT, PATCH or DELETE anywhere in this API.

**Not implemented yet.** Scenario execution returns 501; nothing can run a scenario in this
build. Alerts read the event log honestly and will be empty until a module produces alert
events. Endpoints report the absence of a capability rather than simulating it.
"""


SECURITY_HEADERS = {
    # Nothing loads from anywhere but this origin. The interface bundles its own assets, so
    # there is no CDN to allow, and `frame-ancestors 'none'` is what stops the console being
    # framed by a page that tricks an operator into clicking "acknowledge" on an alert they
    # cannot see. `X-Frame-Options` repeats it for anything too old to read CSP.
    #
    # `'unsafe-inline'` is present for styles and not for scripts: the bundler emits inline
    # style attributes, while every script is a file. Script injection is the one this has to
    # stop, and it does.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "worker-src 'self' blob:; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    # A JSON response that a browser is willing to sniff as HTML is an XSS vector. This says
    # the declared content type is the content type.
    "X-Content-Type-Options": "nosniff",
    # Track identifiers and site names live in URLs. Without this they travel in the Referer
    # header to wherever an operator clicks next.
    "Referrer-Policy": "no-referrer",
    # None of these are used, and a compromised page should not be able to start.
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), usb=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
"""Sent on every response.

Set here rather than left to the reverse proxy. A proxy is the right place for TLS and rate
limiting — things this process genuinely cannot do — but headers it *can* set are headers that
should not depend on someone else's configuration being right. If the proxy sets them too, the
proxy wins and nothing breaks.

`Strict-Transport-Security` is deliberately absent from this dictionary and added only on
requests that arrived over TLS: asserting HSTS over plain HTTP is meaningless, and sending it
from a loopback development run would poison the browser's HSTS cache for `localhost`.
"""

HSTS_HEADER = "max-age=31536000; includeSubDomains"
"""One year, once TLS is genuinely in front. Applied only to requests that arrived over
https — see `SECURITY_HEADERS`."""


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
        # The built-in documentation routes are disabled and re-served below behind a role
        # gate. FastAPI mounts them at the application root with no dependency of their own, so
        # left alone they hand any unauthenticated caller a complete map of every route,
        # parameter and schema in the system. That is standard information disclosure and was
        # caught by the route audit in `tests/integration/test_auth_api.py`, not by review.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.services = services
    app.state.broadcaster = broadcaster

    @app.middleware("http")
    async def _reject_cross_origin_writes(request: Request, call_next):
        """Refuse a state-changing request that a browser says came from somewhere else.

        Defence in depth behind `SameSite=strict`, which is the real protection: a modern
        browser will not attach the session cookie to a cross-site request at all. This covers
        what that does not — a browser too old to enforce SameSite, and any future change that
        loosens the cookie.

        Deliberately permissive in one direction: a request with **no** `Origin` is allowed
        through. Browsers set it on every cross-origin write, so its absence means the caller is
        not a browser — curl, a monitoring probe, the deployment checks in `docs/deployment.md`
        — and those are not the thing CSRF describes. Rejecting them would break legitimate
        automation to defend against an attack they cannot mount.

        The comparison is against the `Host` the request arrived with, so it keeps working
        behind the reverse proxy that terminates TLS without needing to know its name.
        """
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin is not None:
                expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
                # A proxy terminating TLS forwards the original scheme; trust it only when the
                # deployment asserted a proxy is there, which is what proxy_headers does.
                allowed = {expected, expected.replace("http://", "https://", 1)}
                if origin not in allowed:
                    return JSONResponse(
                        status_code=status.HTTP_403_FORBIDDEN,
                        content={"detail": "Cross-origin request refused."},
                    )

        return await call_next(request)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        """Attach the headers above to every response, including errors and static files.

        Middleware rather than a per-route dependency, so a route added later cannot be added
        without them — the same reasoning as gating the router rather than each handler.
        """
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)

        # Only over TLS, and only when a proxy has said so. Sending HSTS from a loopback run
        # would pin `localhost` to https in the developer's browser for a year.
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", HSTS_HEADER)

        return response

    # Auth first: its routes must exist before anything could shadow them, and it is the only
    # router without a baseline role gate.
    app.include_router(auth_routes.router, prefix=API_PREFIX)
    app.include_router(rest.router, prefix=API_PREFIX)
    app.include_router(stream.router, prefix=API_PREFIX)
    app.include_router(system.router, prefix=API_PREFIX)
    _mount_documentation(app)
    return app


def _mount_documentation(app: FastAPI) -> None:
    """Serve the OpenAPI schema and Swagger UI to authenticated callers only.

    The schema describes the software rather than the site's data, which is why `viewer` is
    enough and why this is served at all rather than switched off in production — an API a
    deployment cannot introspect is one nobody can integrate with or audit.

    Swagger UI fetches the schema from the same origin, so the session cookie rides along and
    no token handling is needed in the page.
    """
    schema_url = f"{API_PREFIX}/openapi.json"

    @app.get(schema_url, include_in_schema=False, dependencies=[Depends(require_role(Role.VIEWER))])
    async def openapi_schema() -> JSONResponse:
        return JSONResponse(
            get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
            )
        )

    @app.get(
        f"{API_PREFIX}/docs",
        include_in_schema=False,
        dependencies=[Depends(require_role(Role.VIEWER))],
    )
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url=schema_url, title=f"{app.title} — reference")
