"""Request dependencies: services, the operator, and the role gate.

One module rather than a copy in each router. `_services` and `_operator` were previously
duplicated verbatim in `rest.py` and `system.py`, which was harmless while `_operator` returned
a constant and is not harmless now: two copies of an access check is the arrangement in which
one of them silently stops being updated.

## Fail closed

`require_role` is applied as a **router-level** dependency, so the baseline applies to every
route on that router including ones added later. A route needing more states it; a route
needing less has to say so explicitly and visibly.

The failure this shape exists to prevent is not a wrong role on an existing endpoint — it is a
new endpoint shipping with no gate at all because whoever added it did not know to add one.
`tests/integration/test_auth_api.py` walks the application's own route table and asserts that
every route is either gated or on a short, named list of public ones.

## Every dependency here is `async def`, and must stay that way

FastAPI runs a plain `def` dependency in a worker thread. The persistence layer is one SQLite
connection with no lock (`backend/database/connection.py`), sound only while every use happens
on the event-loop thread — and resolving a session *writes* to it, sliding the expiry. When
`current_operator` was a plain `def`, the console's opening burst of requests reached that
connection from several threads at once; the second `BEGIN IMMEDIATE` landed inside the first
transaction and the request failed with a 500.

`get_services` and the role gate touch no database and are `async def` anyway, so the rule is
one rule. `tests/integration/test_concurrent_requests.py` walks every route and fails on any
handler or dependency that would run in a thread pool.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from backend.api.identity import OperatorContext
from backend.api.services import ApiServices
from backend.auth.models import Role
from backend.auth.policy import SESSION_COOKIE_NAME


async def get_services(request: Request) -> ApiServices:
    return request.app.state.services


ServicesDep = Annotated[ApiServices, Depends(get_services)]


def session_token(request: Request) -> str | None:
    """The presented session token, or None.

    Read from the cookie only. Not from a header, not from a query parameter: a token in a
    query string ends up in access logs, browser history and referer headers, and a token that
    can arrive by two routes is one an attacker gets to choose the route for.
    """
    return request.cookies.get(SESSION_COOKIE_NAME)


async def current_operator(request: Request, services: ServicesDep) -> OperatorContext | None:
    """Resolve the operator, or None when no valid session was presented.

    Returns None rather than raising, so the small number of deliberately public routes can
    call it to *describe* the caller without requiring one.
    """
    return services.identity.resolve(session_token(request))


OptionalOperatorDep = Annotated[OperatorContext | None, Depends(current_operator)]


def require_role(minimum: Role) -> Callable[..., Awaitable[OperatorContext]]:
    """Build a dependency admitting only operators at or above `minimum`.

    Two distinct refusals, and the difference matters to the client:

    - **401** — no valid session. The interface sends the operator to the login page.
    - **403** — a valid session without the necessary role. The interface must *not* send them
      to login, because logging in again will not help and doing so makes a permissions problem
      look like a broken session.
    """

    async def dependency(operator: OptionalOperatorDep) -> OperatorContext:
        if operator is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required.",
                # Deliberately not `WWW-Authenticate: Basic` — that makes browsers pop a native
                # credential dialog the interface cannot style, cannot pre-fill, and cannot
                # dismiss on the operator's behalf.
                headers={"WWW-Authenticate": "Cookie"},
            )

        held = _role_of(operator)
        if held is None or not held.permits(minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"This action requires the {minimum} role or above; "
                    f"{operator.identity.display_name} holds {held or 'no role'}."
                ),
            )

        return operator

    return dependency


def _role_of(operator: OperatorContext) -> Role | None:
    """The operator's role, or None if it is absent or unrecognised.

    An unrecognised value resolves to None and therefore to a refusal. A role string that no
    longer maps to a known role — a level removed in a later version, a typo in a hand-edited
    database — must fail closed. Treating it as "some role, probably fine" is how a retired
    privilege level comes back as a working one.
    """
    raw = operator.identity.role
    if raw is None:
        return None
    try:
        return Role(raw)
    except ValueError:
        return None


RequireViewer = Annotated[OperatorContext, Depends(require_role(Role.VIEWER))]
RequireOperator = Annotated[OperatorContext, Depends(require_role(Role.OPERATOR))]
RequireAdmin = Annotated[OperatorContext, Depends(require_role(Role.ADMIN))]
