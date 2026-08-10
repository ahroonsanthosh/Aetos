"""Login, logout, and "who am I".

Three routes, on their own router with **no baseline role gate** — a login endpoint that
required a session would be a closed loop. Everything else in the API is gated; these are the
documented exceptions, and `tests/integration/test_auth_api.py` names them so the exception
list cannot grow by accident.

## The cookie

`HttpOnly` so script cannot read it, which is what stops cross-site scripting stealing a
session outright. `SameSite=Strict` so a request originating from another site never carries
it, which is the primary defence against cross-site request forgery on the acknowledge
endpoint. `Secure` whenever the connection is TLS.

`SameSite=Strict` rather than a double-submit CSRF token: the interface is same-origin with the
API by construction (`backend/api/static.py` serves the bundle from this process), there is no
cross-origin client to accommodate, and a token adds a second thing that can be misconfigured
for no additional guarantee here. If a cross-origin client ever exists, that trade changes and
a token becomes necessary.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps import OptionalOperatorDep, ServicesDep, session_token
from backend.auth.models import USERNAME_PATTERN, Role
from backend.auth.policy import SESSION_COOKIE_NAME

router = APIRouter()

_AUTH_ABSENT = (
    "Authentication is not configured in this deployment. There is no login to perform, and no "
    "session to end."
)

_REJECTED = "Invalid username or password."
"""One message for every failure.

Wrong password, unknown username, disabled account and locked-out account all produce this. The
event log records which it actually was; the client is told only that it failed. A form that
distinguishes them hands over a list of valid operator usernames, and that is most of the way
to a targeted lockout or a credential-stuffing run.

The lockout case is the one that itches — telling someone "you are locked out for 15 minutes"
is genuinely more useful — but it is also a free oracle for whether a username exists, so the
detail stays in the log where an administrator can see it and an attacker cannot.
"""


class LoginRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    username: str = Field(pattern=USERNAME_PATTERN, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    """Bounded, matching `backend.auth.passwords.MAX_PASSWORD_BYTES`. Rejected at the schema so
    an oversized body never reaches the hashing routine."""


class SessionView(BaseModel):
    """Who the caller is.

    Carries nothing secret — no token, no hash, and no expiry timestamp that would let someone
    holding a stolen cookie work out how long they have.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operator_id: str
    display_name: str
    role: Role
    authenticated: bool


@router.post("/auth/login", response_model=SessionView, tags=["auth"])
async def login(
    request: Request, response: Response, services: ServicesDep, body: LoginRequest
) -> SessionView:
    """Exchange a username and password for a session cookie."""
    if services.auth is None:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, _AUTH_ABSENT)

    result = services.auth.authenticate(body.username, body.password)
    if not result.succeeded or result.user is None or result.token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _REJECTED)

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=result.token,
        httponly=True,
        samesite="strict",
        secure=_is_secure(request),
        path="/",
        # No `max_age`: a session cookie that dies with the browser. Expiry is enforced
        # server-side regardless, and a persistent cookie on a shared console outlives the shift
        # of the person who set it.
    )

    return SessionView(
        operator_id=result.user.user_id,
        display_name=result.user.display_name,
        role=result.user.role,
        authenticated=True,
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
async def logout(request: Request, response: Response, services: ServicesDep) -> None:
    """End the current session.

    Idempotent, and deliberately never an error. Logging out is the action someone takes when
    they are unsure what state they are in, and it must always leave them logged out — an
    already-invalid session answering 401 here would leave a stale cookie in place.
    """
    token = session_token(request)
    if services.auth is not None and token:
        services.auth.logout(token)

    # Cleared with the same attributes it was set with. A browser matches on name, path and
    # domain, so a mismatch silently leaves the cookie where it was.
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=_is_secure(request),
        path="/",
    )


@router.get("/auth/me", response_model=SessionView, tags=["auth"])
async def whoami(operator: OptionalOperatorDep) -> SessionView:
    """Who the caller is, for the interface to decide what to render.

    401 when there is no session, which is what the interface uses on load to choose between the
    console and the login page. Deliberately *not* a 200 carrying `authenticated: false` — a
    success carrying a negative is the shape that leads a client to render a console for nobody.

    What the interface does with the role is presentation only. A viewer is not shown an
    acknowledge button, but the button is not what stops them: `RequireOperator` on the endpoint
    is, and it refuses the request just the same if they craft it by hand.
    """
    if operator is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No session.")

    return SessionView(
        operator_id=operator.identity.operator_id,
        display_name=operator.identity.display_name,
        role=Role(operator.identity.role) if operator.identity.role else Role.VIEWER,
        authenticated=operator.identity.authenticated,
    )


def _is_secure(request: Request) -> bool:
    """Whether the cookie should be marked `Secure`.

    Purely the connection's scheme. That is sufficient *because of* the startup guard, not
    despite it: `guard_bind` in `backend/main.py` refuses to bind anything but loopback without
    `--behind-proxy`, and `--behind-proxy` is what enables uvicorn's `proxy_headers`, which
    rewrites this scheme to https from the proxy's `X-Forwarded-Proto`. So the only way to be
    served over plain http is to be on loopback, where `Secure` would be actively harmful — the
    browser would refuse to send the cookie back, and login would appear to succeed and then
    silently do nothing.

    Deciding from the hostname instead was tried and is wrong: it marks the cookie `Secure` for
    any name that is not literally `localhost`, which breaks a deployment reached by machine
    name over a plain-http tunnel, and produces exactly the silent no-op above.
    """
    return request.url.scheme == "https"
