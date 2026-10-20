"""Every route the application actually serves, with its full path.

Audits that walk the route table must not walk `app.routes` directly. From FastAPI 0.14x an
included router is kept as one opaque entry rather than flattened into its routes, so
`app.routes` lists only the handful registered on the app itself. The access-control audit in
`test_auth_api.py` walked it that way and went on passing while checking 3 of 22 routes — none
of them a data endpoint — because a test that finds nothing to check finds nothing wrong.

`fastapi.routing.iter_route_contexts` is the public walk FastAPI's own OpenAPI generator uses.
Older FastAPI flattened included routers, so there `app.routes` is already complete; the
fallback keeps the helper correct across the range `pyproject.toml` admits.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI


@dataclass(frozen=True)
class EffectiveRoute:
    path: str
    methods: frozenset[str]
    name: str
    route: Any
    """The underlying route object — an `APIRoute`, `APIWebSocketRoute`, `Mount` and so on."""


def effective_routes(app: FastAPI) -> Iterator[EffectiveRoute]:
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # FastAPI before included routers stopped being flattened
        for route in app.routes:
            yield EffectiveRoute(
                path=getattr(route, "path", ""),
                methods=frozenset(getattr(route, "methods", None) or ()),
                name=getattr(route, "name", "") or "",
                route=route,
            )
        return

    for context in iter_route_contexts(app.routes):
        yield EffectiveRoute(
            path=context.path or "",
            methods=frozenset(context.methods or ()),
            name=context.name or "",
            route=context.route,
        )
