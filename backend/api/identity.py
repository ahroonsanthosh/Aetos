"""Operator identity — a seam, deliberately not an authentication system.

**Aetos has no authentication and no authorisation checks.** There is no login, no session, no
password, no role. Every operator action is attributed to one configured local operator, and
that identity is *asserted*, not authenticated. See ADR 0008 and FM-001.

This is a decision, not an omission. A half-built authentication system is worse than none,
because it looks like security: it invites the system to be exposed on the assumption that
something is checking, when nothing is. So the seam exists — every operator action carries an
`OperatorContext`, and `OPERATOR_ACKNOWLEDGED` events will be attributable in structure from
the first one ever written — and behind the seam there is a single configured identity and
nothing else.

The consequence is stated plainly: **the event log does not currently prove who did
something**, and Aetos must not be exposed to an untrusted network in this state.

The one property that is enforced rather than promised: `resolve()` takes no request and reads
nothing a client sends. There is no header, cookie or body field by which a caller can name
itself, so no client assertion of identity can reach a stored record (spec §14).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.common.clock import Clock

DEFAULT_OPERATOR_ID = "operator-local"
DEFAULT_OPERATOR_NAME = "Local operator"


class Identity(BaseModel):
    """Who an action is attributed to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operator_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)

    authenticated: bool = False
    """Whether this identity was *proved* rather than assumed.

    Always False today, and carried explicitly rather than left implicit so that a future
    audit export cannot accidentally present an assumed identity as a verified one. The day
    authentication exists, this becomes the field that distinguishes the two eras of the log.
    """


class OperatorContext(BaseModel):
    """An identity together with when and on what basis it was asserted.

    The basis is recorded because it is currently the honest answer to "how do you know it was
    them": you do not. `local-configured` says so in one word.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: Identity
    asserted_at: datetime
    basis: str = Field(min_length=1, max_length=64)


class IdentityProvider(ABC):
    """Resolves the operator an action is attributed to.

    Takes no request argument, on purpose. An implementation that read a header would be
    accepting a client's claim about who it is, which with no authentication behind it is not
    a weaker guarantee than the current one — it is a false one.
    """

    @abstractmethod
    def resolve(self) -> OperatorContext:
        ...


class LocalOperatorIdentityProvider(IdentityProvider):
    """The V0 provider: one configured local operator, always.

    Every action in the system is attributed to this identity. Two operators sharing a
    deployment are indistinguishable in the record, and the record says so through
    `authenticated=False` and `basis="local-configured"` rather than leaving a reader to
    assume otherwise.
    """

    def __init__(
        self,
        clock: Clock,
        operator_id: str = DEFAULT_OPERATOR_ID,
        display_name: str = DEFAULT_OPERATOR_NAME,
    ) -> None:
        self._clock = clock
        self._identity = Identity(operator_id=operator_id, display_name=display_name)

    def resolve(self) -> OperatorContext:
        return OperatorContext(
            identity=self._identity,
            asserted_at=self._clock.now(),
            basis="local-configured",
        )
