"""How long a session lives, and how hard it is to guess a password.

Configuration rather than constants, for the reason every other policy in this repository is:
these are a site's judgement, and the people best placed to tighten them are not the people who
can edit Python. A site guarding a stadium on an event day and a test range on a Tuesday
disagree about idle timeout, and neither should have to fork the software to say so.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from backend.auth.passwords import DEFAULT_N, DEFAULT_P, DEFAULT_R, ScryptParameters
from backend.common.config import ConfigError, ConfigPaths

DEFAULT_POLICY_FILE = "policy.json"

SESSION_COOKIE_NAME = "aetos_session"
"""Fixed, not configurable. The frontend has to know it too, and a name that can differ between
the two ends is a misconfiguration that presents as 'login silently does nothing'."""


class AuthPolicy(BaseModel):
    """Session lifetime, lockout, and password work factor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    notes: str = ""
    """Free text for whoever edits this file. Declared because `extra="forbid"` would otherwise
    reject a `_comment` key."""

    idle_timeout_s: float = Field(default=3600.0, gt=0)
    """How long a session survives without activity.

    Sliding: every authenticated request pushes it out. An operator working an incursion is
    never logged out mid-incident, which is a safety property and not only a convenience — an
    operator forced to re-authenticate while tracking a contact is an operator not tracking a
    contact.
    """

    absolute_timeout_s: float = Field(default=43200.0, gt=0)
    """Hard ceiling from login, regardless of activity.

    The reason the idle timeout alone is not enough: a stolen session cookie kept warm by an
    attacker's own traffic would otherwise never expire. Twelve hours by default, which covers a
    long shift without spanning two.
    """

    max_failed_attempts: int = Field(default=5, ge=1)
    lockout_duration_s: float = Field(default=900.0, gt=0)
    """How long an account is locked after `max_failed_attempts` failures.

    Deliberately short and self-clearing rather than a lock needing an administrator. Lockout is
    a denial-of-service lever pointed at a legitimate operator — anyone who knows a username can
    trip it — so on a console that may be the only way to see the sky, a lockout that outlives
    the attack is its own hazard. Fifteen minutes makes online guessing hopeless while keeping
    the console recoverable without a second person.
    """

    scrypt_n: int = Field(default=DEFAULT_N, ge=2**12)
    scrypt_r: int = Field(default=DEFAULT_R, ge=1)
    scrypt_p: int = Field(default=DEFAULT_P, ge=1)
    """Password work factor. Raising these is safe: hashes carry the parameters they were made
    with, and each account is re-hashed on its next successful login."""

    def model_post_init(self, _context: object) -> None:
        if self.absolute_timeout_s < self.idle_timeout_s:
            raise ValueError(
                f"absolute_timeout_s ({self.absolute_timeout_s}) is below idle_timeout_s "
                f"({self.idle_timeout_s}); the idle timeout could then never be reached, which "
                "is not what either setting is for"
            )
        if self.scrypt_n & (self.scrypt_n - 1):
            raise ValueError(f"scrypt_n ({self.scrypt_n}) must be a power of two")

    @property
    def scrypt(self) -> ScryptParameters:
        return ScryptParameters(n=self.scrypt_n, r=self.scrypt_r, p=self.scrypt_p)


def load_auth_policy(
    paths: ConfigPaths | None = None, filename: str = DEFAULT_POLICY_FILE
) -> AuthPolicy:
    """Read the auth policy from `config/auth/`.

    A missing file is an error, not a fall back to defaults. Silent defaults in an access
    control policy are the kind of thing a deployment discovers during an incident.
    """
    resolved = paths or ConfigPaths.default()
    file = resolved.config_root / "auth" / filename

    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{file}: auth policy not found") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{file}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{file}: could not be read: {exc}") from exc

    try:
        return AuthPolicy.model_validate(document)
    except Exception as exc:  # pydantic ValidationError, reported with the file that caused it
        raise ConfigError(f"{file}: invalid auth policy: {exc}") from exc


def policy_path(paths: ConfigPaths | None = None) -> Path:
    """Where the policy is read from. Exposed for diagnostics and tests."""
    resolved = paths or ConfigPaths.default()
    return resolved.config_root / "auth" / DEFAULT_POLICY_FILE
