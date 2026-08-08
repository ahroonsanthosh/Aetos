"""REST and WebSocket surface. Reads state; never owns it.

See `backend/api/README.md`. Two things to know before using this module: there is no
authentication (ADR 0008, FM-001), and the WebSocket stream is fed by reading the event log by
sequence number rather than by producers pushing into it (ADR 0006).
"""

from backend.api.app import create_app
from backend.api.identity import (
    Identity,
    IdentityProvider,
    LocalOperatorIdentityProvider,
    OperatorContext,
)
from backend.api.schemas import API_PREFIX, API_VERSION
from backend.api.services import ApiServices
from backend.api.stream import STREAMED_EVENT_TYPES, EventBroadcaster

__all__ = [
    "API_PREFIX",
    "API_VERSION",
    "STREAMED_EVENT_TYPES",
    "ApiServices",
    "EventBroadcaster",
    "Identity",
    "IdentityProvider",
    "LocalOperatorIdentityProvider",
    "OperatorContext",
    "create_app",
]
