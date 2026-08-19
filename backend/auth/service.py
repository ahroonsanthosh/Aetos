"""Authentication and session lifecycle.

Everything that decides whether someone gets in, and for how long. Three properties are worth
stating up front because they are easy to lose in a later edit:

1. **A failure never says why, to the client.** Wrong password, unknown username, disabled
   account and locked-out account all return the same thing. The event log records which it
   was; the caller learns only that it failed. Anything finer is an oracle — a login form that
   distinguishes "no such user" from "wrong password" hands over a list of real operator
   usernames, which is most of the way to a targeted lockout.
2. **Timing does not say why either.** An unknown username still pays the cost of a password
   hash (`dummy_verify`), so the response time of a real account and a fictional one are the
   same to within noise. Measured at 392 ms against 397 ms, inside the run-to-run spread.
3. **Time comes from the injected `Clock`.** Not a style rule here —
   `tests/unit/test_module_boundaries.py` AST-scans for `time.monotonic` and friends and fails
   the build. The upside is that session expiry and lockout windows are testable with
   `SimClock` instead of by sleeping.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, auto

from backend.auth.models import Credential, Role, Session, User
from backend.auth.passwords import dummy_verify, hash_password, needs_rehash, verify_password
from backend.auth.policy import AuthPolicy
from backend.common.clock import Clock
from backend.common.identifiers import IdGenerator
from backend.database.repositories import (
    CredentialRepository,
    SessionRepository,
    UserRepository,
)
from backend.events.store import EventStore
from backend.events.types import EventType

SOURCE = "auth"

_USER_PREFIX = "USR"
_SESSION_PREFIX = "SES"

TOKEN_BYTES = 32
"""256 bits from a CSPRNG. Large enough that guessing is not a threat model, which is why the
stored hash needs no key-stretching — see `Session.token_hash`."""


class LoginFailure(StrEnum):
    """Why an authentication attempt failed. **Recorded, never returned to the client.**"""

    NO_SUCH_USER = auto()
    WRONG_PASSWORD = auto()
    ACCOUNT_DISABLED = auto()
    ACCOUNT_LOCKED = auto()


@dataclass(frozen=True, slots=True)
class AuthenticatedOperator:
    """A validated session and the account behind it."""

    user: User
    session: Session


@dataclass(frozen=True, slots=True)
class LoginResult:
    """The outcome of an attempt.

    `token` is present exactly when `user` is, and is the only time the raw token exists
    outside the client's cookie — it is not stored, not logged, and not recoverable.
    """

    token: str | None = None
    user: User | None = None
    failure: LoginFailure | None = None

    @property
    def succeeded(self) -> bool:
        return self.token is not None and self.user is not None


class AuthService:
    """Authenticates, issues and validates sessions, and manages accounts."""

    def __init__(
        self,
        policy: AuthPolicy,
        clock: Clock,
        users: UserRepository,
        credentials: CredentialRepository,
        sessions: SessionRepository,
        events: EventStore,
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._users = users
        self._credentials = credentials
        self._sessions = sessions
        self._events = events

        # Resumed past what is already stored, exactly as the scenario repository does. A
        # generator restarting at 1 would mint an existing `user_id` after a restart, and
        # `upsert` would then silently overwrite a real account — including its role. Accounts
        # are never deleted, so the count is a sound resume point.
        self._user_ids = IdGenerator(_USER_PREFIX, width=5, start=users.count() + 1)

    # -----------------------------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------------------------

    def authenticate(self, username: str, password: str) -> LoginResult:
        """Check a credential and, on success, issue a session.

        Every branch that fails does the same amount of hashing work and returns the same
        shape. Read the ordering carefully before changing it: the lockout check comes *before*
        password verification, so a locked account cannot be probed for the correct password;
        the disabled check comes after verification, so whether an account is disabled is not
        learnable without already knowing its password.
        """
        now = self._clock.now()
        user = self._users.by_username(username.strip().lower())

        if user is None:
            # Hash anyway. Returning here without the work would make an unknown username
            # measurably faster than a known one.
            dummy_verify(self._policy.scrypt)
            return self._failed(username, None, LoginFailure.NO_SUCH_USER)

        if user.locked_at(now):
            dummy_verify(self._policy.scrypt)
            return self._failed(username, user, LoginFailure.ACCOUNT_LOCKED)

        credential = self._credentials.get(user.user_id)
        if credential is None or not verify_password(password, credential.password_hash):
            if credential is None:
                # An account with no credential cannot be logged into, but must still cost the
                # same as one that can.
                dummy_verify(self._policy.scrypt)
            return self._record_bad_password(username, user, now)

        if not user.enabled:
            return self._failed(username, user, LoginFailure.ACCOUNT_DISABLED)

        self._clear_failures(user, now)
        self._upgrade_hash_if_stale(user, password, credential, now)

        token, session = self._issue_session(user, now)
        self._events.append(
            EventType.OPERATOR_LOGIN_SUCCEEDED,
            SOURCE,
            payload={
                "user_id": user.user_id,
                "username": user.username,
                "role": str(user.role),
                "session_id": session.session_id,
            },
        )
        return LoginResult(token=token, user=user)

    def validate(self, token: str) -> AuthenticatedOperator | None:
        """Resolve a session token to an operator, or None.

        Slides the idle expiry as a side effect, which is what makes an operator working an
        incursion immune to being logged out mid-incident.

        Note the account is re-read on every request rather than trusted from the session. A
        role changed or an account disabled while someone is logged in has to take effect
        immediately, and a session carrying a cached role would keep the old one until the
        holder happened to log out.
        """
        if not token:
            return None

        session = self._sessions.by_token_hash(hash_token(token))
        if session is None:
            return None

        now = self._clock.now()
        if not session.is_live(now, self._policy.idle_timeout_s):
            self._sessions.revoke(session.session_id)
            return None

        user = self._users.get(session.user_id)
        if user is None or not user.enabled:
            self._sessions.revoke(session.session_id)
            return None

        self._sessions.touch(session.session_id, now)
        return AuthenticatedOperator(
            user=user, session=session.model_copy(update={"last_seen_at": now})
        )

    def logout(self, token: str) -> bool:
        """End a session. Returns whether one was ended."""
        session = self._sessions.by_token_hash(hash_token(token))
        if session is None:
            return False

        self._sessions.revoke(session.session_id)
        user = self._users.get(session.user_id)
        self._events.append(
            EventType.OPERATOR_LOGGED_OUT,
            SOURCE,
            payload={
                "user_id": session.user_id,
                "username": user.username if user else None,
                "session_id": session.session_id,
            },
        )
        return True

    # -----------------------------------------------------------------------------------
    # Accounts
    # -----------------------------------------------------------------------------------

    def create_user(
        self, username: str, password: str, role: Role, display_name: str | None = None
    ) -> User:
        """Create an account with a password. Raises `ValueError` if the username is taken."""
        normalised = username.strip().lower()
        if self._users.by_username(normalised) is not None:
            raise ValueError(f"username {normalised!r} already exists")

        now = self._clock.now()
        user = User(
            user_id=self._user_ids.next(),
            username=normalised,
            display_name=display_name or normalised,
            role=role,
            created_at=now,
            updated_at=now,
        )
        self._users.upsert(user)
        self.set_password(user.user_id, password)

        self._events.append(
            EventType.USER_CREATED,
            SOURCE,
            payload={
                "user_id": user.user_id,
                "username": user.username,
                "role": str(user.role),
            },
        )
        return user

    def set_password(self, user_id: str, password: str) -> None:
        """Set or replace an account's password.

        Does **not** revoke the account's sessions. Someone changing their own password should
        not be logged out of the console they are sitting at; an administrator resetting
        someone else's should disable and re-enable the account, which revokes them.
        """
        now = self._clock.now()
        self._credentials.set_for_user(
            Credential(
                user_id=user_id,
                password_hash=hash_password(password, self._policy.scrypt),
                updated_at=now,
            )
        )
        self._events.append(EventType.USER_PASSWORD_CHANGED, SOURCE, payload={"user_id": user_id})

    def set_role(self, user_id: str, role: Role) -> User | None:
        user = self._users.get(user_id)
        if user is None:
            return None

        previous = user.role
        updated = user.model_copy(update={"role": role, "updated_at": self._clock.now()})
        self._users.upsert(updated)
        self._events.append(
            EventType.USER_ROLE_CHANGED,
            SOURCE,
            payload={
                "user_id": user_id,
                "username": user.username,
                "role": str(role),
                "previous_role": str(previous),
            },
        )
        return updated

    def set_enabled(self, user_id: str, enabled: bool) -> User | None:
        """Enable or disable an account.

        Disabling revokes every session it holds, in one operation. An account judged unfit to
        log in that keeps a live session until it happens to expire has not been disabled.
        """
        user = self._users.get(user_id)
        if user is None:
            return None

        updated = user.model_copy(update={"enabled": enabled, "updated_at": self._clock.now()})
        self._users.upsert(updated)

        revoked = 0 if enabled else self._sessions.revoke_all_for_user(user_id)
        self._events.append(
            EventType.USER_ENABLEMENT_CHANGED,
            SOURCE,
            payload={
                "user_id": user_id,
                "username": user.username,
                "enabled": enabled,
                "sessions_revoked": revoked,
            },
        )
        return updated

    def revoke_sessions(self, user_id: str) -> int:
        return self._sessions.revoke_all_for_user(user_id)

    def clear_lockout(self, user_id: str) -> User | None:
        """Let a locked-out account try again before its window elapses."""
        user = self._users.get(user_id)
        if user is None:
            return None

        updated = user.model_copy(
            update={
                "failed_attempts": 0,
                "locked_until": None,
                "updated_at": self._clock.now(),
            }
        )
        self._users.upsert(updated)
        return updated

    def purge_expired_sessions(self) -> int:
        return self._sessions.purge_expired(self._clock.now())

    def user_count(self) -> int:
        return self._users.count()

    # -----------------------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------------------

    def _issue_session(self, user: User, now: datetime) -> tuple[str, Session]:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        session = Session(
            # Random rather than sequential, unlike every other identifier in this codebase.
            # Sessions are the one kind of row that is genuinely deleted, so a counter would
            # hand a fresh session the identifier of one already named in the log — and two
            # different logins sharing a `session_id` makes the audit trail ambiguous exactly
            # where it needs to be precise. Sessions take no part in replay, so the determinism
            # the sequential generators exist for does not apply.
            session_id=f"{_SESSION_PREFIX}-{secrets.token_hex(8)}",
            user_id=user.user_id,
            token_hash=hash_token(token),
            created_at=now,
            last_seen_at=now,
            absolute_expiry=now + timedelta(seconds=self._policy.absolute_timeout_s),
        )
        self._sessions.create(session)
        return token, session

    def _record_bad_password(self, username: str, user: User, now: datetime) -> LoginResult:
        """Count the failure, and lock the account if it crossed the threshold."""
        attempts = user.failed_attempts + 1
        locked = attempts >= self._policy.max_failed_attempts

        self._users.upsert(
            user.model_copy(
                update={
                    "failed_attempts": 0 if locked else attempts,
                    "locked_until": (
                        now + timedelta(seconds=self._policy.lockout_duration_s)
                        if locked
                        else user.locked_until
                    ),
                    "updated_at": now,
                }
            )
        )

        # The failure is recorded before the lockout it caused, so the log reads in the order
        # the events happened rather than leading with the consequence.
        result = self._failed(username, user, LoginFailure.WRONG_PASSWORD)

        if locked:
            self._events.append(
                EventType.OPERATOR_LOCKED_OUT,
                SOURCE,
                payload={
                    "user_id": user.user_id,
                    "username": user.username,
                    "failed_attempts": attempts,
                    "locked_for_s": self._policy.lockout_duration_s,
                },
            )

        return result

    def _clear_failures(self, user: User, now: datetime) -> None:
        if user.failed_attempts == 0 and user.locked_until is None:
            return
        self._users.upsert(
            user.model_copy(update={"failed_attempts": 0, "locked_until": None, "updated_at": now})
        )

    def _upgrade_hash_if_stale(
        self, user: User, password: str, credential: Credential, now: datetime
    ) -> None:
        """Re-hash at the current work factor, if the stored one is weaker.

        This moment — just after a successful verification — is the only time the plaintext is
        available, and therefore the only opportunity to raise the work factor without asking
        everyone to reset their password.
        """
        if not needs_rehash(credential.password_hash, self._policy.scrypt):
            return

        self._credentials.set_for_user(
            Credential(
                user_id=user.user_id,
                password_hash=hash_password(password, self._policy.scrypt),
                updated_at=now,
            )
        )

    def _failed(self, username: str, user: User | None, why: LoginFailure) -> LoginResult:
        self._events.append(
            EventType.OPERATOR_LOGIN_FAILED,
            SOURCE,
            payload={
                # The username attempted, and nothing that was supplied with it.
                "username": username[:64],
                "user_id": user.user_id if user else None,
                "reason": str(why),
            },
        )
        return LoginResult(failure=why)


def hash_token(token: str) -> str:
    """SHA-256 of a session token, hex.

    A plain digest rather than a password hash: the token is 256 bits of CSPRNG output, so it is
    not guessable and stretching would only slow every authenticated request down. The hash
    exists so that reading the database — a backup, a stolen file, a support dump — yields no
    usable session.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
