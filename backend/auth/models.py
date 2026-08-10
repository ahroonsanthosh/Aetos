"""What an account, a credential and a session are.

Three types rather than one, and the split is load-bearing:

- `User` is who someone is and what they may do. It is safe to serialise.
- `Credential` is the secret. It lives in its own table and is never reachable from a `User`.
- `Session` is a proved claim with an expiry.

Keeping the credential out of `User` is not tidiness. The storage convention in this codebase
puts a model's JSON in a `document` column and re-validates it on read, so a hash stored as a
`User` field would round-trip through the same object the API serialises — and one careless
response model would leak every credential at once. Separate types make that leak structurally
impossible instead of a thing someone has to remember.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

AUTH_SCHEMA_VERSION = "1.0.0"

USERNAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,63}$"
"""Usernames are lowercase and bounded.

Constrained at the model rather than checked at the call site because a username reaches a SQL
parameter, an event payload and a log line, and the cheapest place to make all three safe is
the one place it is constructed.
"""


class Role(StrEnum):
    """What an account may do.

    Ordered, not a set of flags. `rank` exists so a check reads "operator or above" rather than
    enumerating the roles that qualify — an enumeration silently omits any role added later,
    and it fails *open* for the new one, which is the wrong direction to fail.
    """

    VIEWER = "viewer"
    """Reads the picture. Cannot change anything."""

    OPERATOR = "operator"
    """Reads, and acknowledges alerts. The role an actual console user holds."""

    ADMIN = "admin"
    """Also manages accounts."""

    @property
    def rank(self) -> int:
        return {"viewer": 0, "operator": 1, "admin": 2}[self.value]

    def permits(self, required: Role) -> bool:
        """Whether this role satisfies a requirement of at least `required`."""
        return self.rank >= required.rank


class User(BaseModel):
    """An account. Carries no secret and never will.

    Lockout state lives here because it is a property of the account rather than of any one
    request, and because it has to survive a restart: a counter held in memory resets when the
    process bounces, which hands an attacker unlimited attempts for the price of waiting for a
    deploy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    username: str = Field(pattern=USERNAME_PATTERN, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    role: Role

    enabled: bool = True
    """A disabled account keeps its history and its attribution in the log, but cannot
    authenticate. Deleting it instead would orphan every event it is named in."""

    created_at: datetime
    updated_at: datetime

    failed_attempts: int = Field(default=0, ge=0)
    locked_until: datetime | None = None

    schema_version: str = AUTH_SCHEMA_VERSION

    def locked_at(self, now: datetime) -> bool:
        return self.locked_until is not None and now < self.locked_until


class Credential(BaseModel):
    """A stored password verifier.

    `password_hash` is the encoded scrypt string from `backend.auth.passwords`, carrying its
    own parameters so they can be raised later without invalidating what is already stored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    password_hash: str = Field(min_length=1, max_length=512)
    updated_at: datetime


class Session(BaseModel):
    """A proved claim to an identity, with two independent expiries.

    Only `token_hash` is ever stored. The token itself is returned once, to the client that
    authenticated, and never written anywhere — so a database file, a backup or a support dump
    yields no live sessions. This costs nothing at the start and cannot be retrofitted quietly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)

    token_hash: str = Field(min_length=1, max_length=128)
    """SHA-256 of the session token, hex. Not a password hash: the token is 256 bits of entropy
    from a CSPRNG, so it is not guessable and needs no key-stretching — the hash is here to stop
    a database read yielding usable tokens, nothing more."""

    created_at: datetime
    last_seen_at: datetime

    absolute_expiry: datetime
    """Hard ceiling, fixed at login. Sliding renewal never moves it, so a session cannot be kept
    alive indefinitely by activity alone."""

    def expires_at(self, idle_timeout_s: float) -> datetime:
        """The earlier of the idle deadline and the absolute ceiling."""
        return min(self.last_seen_at + timedelta(seconds=idle_timeout_s), self.absolute_expiry)

    def is_live(self, now: datetime, idle_timeout_s: float) -> bool:
        return now < self.expires_at(idle_timeout_s)
