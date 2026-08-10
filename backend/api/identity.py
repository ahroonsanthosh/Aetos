"""Operator identity — who an action is attributed to, and on what basis.

Two providers live here, and which one is in use is the difference between two eras of the
event log:

- `SessionIdentityProvider` resolves from a **validated session** (ADR 0016). Actions are
  attributed to a real account that presented a credential, `Identity.authenticated` is true,
  and `OperatorContext.basis` is `session`. This is what makes the log able to answer *who*.
- `LocalOperatorIdentityProvider` attributes everything to one configured identity with no
  login at all (ADR 0008, FM-001). Retained for single-user local development and for tests,
  and it says what it is: `authenticated=False`, `basis="local-configured"`.

A note on the interface, because it changed and the original reasoning still matters.
`resolve()` used to take no argument on purpose, and the docstring argued that an
implementation reading a header would be "accepting a client's claim about who it is". That
argument was right for a system with nothing to check the claim against, and it does **not**
apply to a validated session: the cookie is not believed, it is looked up, and what comes back
is server-side state the client cannot author. The distinction between *accepting an assertion*
and *resolving a proof* is the whole of what this module now does, so `resolve()` takes the
presented token and returns nothing at all when it does not resolve.

What has not changed: no header, body field or query parameter can name an operator. The only
thing a client may present is an opaque token it was given in exchange for a password.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.auth.service import AuthService
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

    Carried explicitly rather than left implicit so an audit export cannot present an assumed
    identity as a verified one. This is the field that distinguishes the two eras of the log:
    false for everything recorded before ADR 0016, true for anything attributed to an account
    that presented a credential. It defaults to false so a provider that forgets to set it
    under-claims rather than over-claims.
    """

    role: str | None = None
    """The account's role at the moment of the action, or None when unauthenticated.

    Recorded per action rather than looked up later, because roles change: an export that
    resolved a role at read time would report what someone may do *now* as though it were what
    they were permitted to do *then*.
    """


class OperatorContext(BaseModel):
    """An identity together with when and on what basis it was established.

    `basis` is recorded because "how do you know it was them" has more than one answer, and a
    log spanning a deployment's history will contain both. `session` means a credential was
    checked. `local-configured` means it was not, and is the honest one-word admission that the
    attribution is structural only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: Identity
    asserted_at: datetime
    basis: str = Field(min_length=1, max_length=64)


class IdentityProvider(ABC):
    """Resolves the operator an action is attributed to.

    `token` is whatever opaque credential the client presented, or None if it presented none.
    Returning None means *no operator* — not an anonymous one — and callers must treat it as a
    refusal rather than substituting a default.
    """

    @abstractmethod
    def resolve(self, token: str | None = None) -> OperatorContext | None:
        ...

    @property
    @abstractmethod
    def requires_credential(self) -> bool:
        """Whether this provider actually checks anything.

        Exposed so `GET /system/health` can report the deployment's access-control posture from
        a fact rather than a constant, and so the startup guard can refuse to bind to a network
        interface with an unauthenticated provider in place.
        """


class SessionIdentityProvider(IdentityProvider):
    """Resolves an identity from a validated session (ADR 0016).

    Holds no state of its own: every call goes to `AuthService.validate`, which re-reads the
    account rather than trusting anything cached on the session. A role revoked or an account
    disabled mid-shift therefore takes effect on the next request, not at the next login.
    """

    def __init__(self, clock: Clock, auth: AuthService) -> None:
        self._clock = clock
        self._auth = auth

    @property
    def requires_credential(self) -> bool:
        return True

    def resolve(self, token: str | None = None) -> OperatorContext | None:
        if not token:
            return None

        authenticated = self._auth.validate(token)
        if authenticated is None:
            return None

        return OperatorContext(
            identity=Identity(
                operator_id=authenticated.user.user_id,
                display_name=authenticated.user.display_name,
                authenticated=True,
                role=str(authenticated.user.role),
            ),
            asserted_at=self._clock.now(),
            basis="session",
        )


class LocalOperatorIdentityProvider(IdentityProvider):
    """One configured local operator, always, with no login (ADR 0008, FM-001).

    Every action is attributed to this identity, so two people sharing a deployment are
    indistinguishable in the record — and the record says so through `authenticated=False` and
    `basis="local-configured"` rather than leaving a reader to assume otherwise.

    Still here because it is the right thing for a single-user machine and for tests, and
    because deleting it would force those to carry a password. It is **not** safe on a network,
    and `requires_credential` returning False is what lets the startup guard in
    `backend/main.py` refuse that combination rather than rely on anyone reading this paragraph.
    """

    def __init__(
        self,
        clock: Clock,
        operator_id: str = DEFAULT_OPERATOR_ID,
        display_name: str = DEFAULT_OPERATOR_NAME,
        role: str = "admin",
    ) -> None:
        self._clock = clock
        self._identity = Identity(
            operator_id=operator_id, display_name=display_name, role=role
        )

    @property
    def requires_credential(self) -> bool:
        return False

    def resolve(self, token: str | None = None) -> OperatorContext | None:
        """Ignores the token: there is nothing to check it against."""
        return OperatorContext(
            identity=self._identity,
            asserted_at=self._clock.now(),
            basis="local-configured",
        )
