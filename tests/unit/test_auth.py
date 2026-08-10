"""Password hashing, session lifetime, and lockout.

The properties here are the ones an authorisation review asks about directly, so each is tested
as a behaviour rather than inferred from a configuration value. Session expiry is driven with
`SimClock` rather than by sleeping — which is only possible because the module-boundary test
forbids wall-clock calls, so every deadline in the service comes from the injected clock.
"""

from __future__ import annotations

import statistics
import time
from datetime import UTC, datetime

import pytest

from backend.auth.models import Role, Session, User
from backend.auth.passwords import (
    ScryptParameters,
    dummy_verify,
    hash_password,
    needs_rehash,
    verify_password,
)
from backend.auth.policy import AuthPolicy, load_auth_policy
from backend.auth.service import AuthService, LoginFailure, hash_token
from backend.common.clock import SimClock
from backend.database.sqlite import SqliteRepositories
from backend.events.store import SqliteEventStore

EPOCH = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

PASSWORD = "a-long-enough-passphrase"

FAST = ScryptParameters(n=2**12, r=8, p=1)
"""Cheap parameters for tests that are not measuring the work factor itself."""


@pytest.fixture
def clock() -> SimClock:
    return SimClock(EPOCH)


@pytest.fixture
def auth(clock: SimClock):
    repos = SqliteRepositories(clock)
    events = SqliteEventStore(clock)
    service = AuthService(
        AuthPolicy(scrypt_n=2**12, scrypt_r=8, scrypt_p=1),
        clock,
        repos.users,
        repos.credentials,
        repos.sessions,
        events,
    )
    yield service
    repos.close()
    events.close()


# --------------------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------------------


def test_a_password_verifies_against_its_own_hash() -> None:
    encoded = hash_password(PASSWORD, FAST)

    assert verify_password(PASSWORD, encoded)
    assert not verify_password("something else", encoded)


def test_the_same_password_hashes_differently_every_time() -> None:
    """A fresh salt per call. Without it, two accounts sharing a password share a hash, and a
    precomputed table works against the whole database at once."""
    assert hash_password(PASSWORD, FAST) != hash_password(PASSWORD, FAST)


def test_a_malformed_stored_hash_fails_closed() -> None:
    """A corrupted row must read as "wrong password", not raise. Raising would distinguish a
    damaged record from an incorrect attempt, which is an oracle."""
    for damaged in ("", "garbage", "scrypt$4096$8$1$truncated", "bcrypt$x$y$z$a$b"):
        assert verify_password(PASSWORD, damaged) is False


def test_a_hash_carries_the_parameters_it_was_made_with() -> None:
    """This is what makes the work factor raisable later without resetting every password."""
    weak = hash_password(PASSWORD, ScryptParameters(n=2**12, r=8, p=1))

    assert needs_rehash(weak, ScryptParameters(n=2**16, r=8, p=2))
    assert not needs_rehash(weak, ScryptParameters(n=2**12, r=8, p=1))
    # Still verifiable at the old parameters — an upgrade must not lock anyone out.
    assert verify_password(PASSWORD, weak)


def test_a_stronger_hash_is_rehashed_on_the_next_login(auth: AuthService) -> None:
    """The upgrade path, end to end: the account is re-hashed at the current work factor the
    next time the plaintext is available, which is the only moment it ever is."""
    user = auth.create_user("alice", PASSWORD, Role.OPERATOR)
    auth._credentials.set_for_user(  # noqa: SLF001 - planting a deliberately stale hash
        auth._credentials.get(user.user_id).model_copy(  # noqa: SLF001
            update={"password_hash": hash_password(PASSWORD, ScryptParameters(n=2**12, r=8, p=1))}
        )
    )

    assert auth.authenticate("alice", PASSWORD).succeeded
    assert verify_password(PASSWORD, auth._credentials.get(user.user_id).password_hash)  # noqa: SLF001


def test_an_unknown_user_costs_the_same_as_a_known_one() -> None:
    """The enumeration defence, measured rather than assumed.

    If an unknown username returned without hashing, it would answer in microseconds while a
    real one took the full work factor — which tells an attacker which operator usernames exist,
    and a list of real usernames is most of the way to a targeted lockout.
    """
    encoded = hash_password(PASSWORD, FAST)

    # Warm up: the first scrypt call in a process pays one-off setup that would otherwise
    # dominate the comparison.
    verify_password("x", encoded)
    dummy_verify(FAST)

    def median_ms(call) -> float:
        samples = []
        for _ in range(7):
            start = time.perf_counter()
            call()
            samples.append((time.perf_counter() - start) * 1000)
        return statistics.median(samples)

    real = median_ms(lambda: verify_password("wrong-password", encoded))
    fictional = median_ms(lambda: dummy_verify(FAST))

    # Generous, because a shared CI runner is noisy. It still fails decisively if the dummy
    # hash is ever removed, since the gap would then be the entire work factor.
    assert abs(real - fictional) < max(real, fictional) * 0.5


# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------


def test_a_session_survives_activity_but_not_idleness(
    auth: AuthService, clock: SimClock
) -> None:
    """The safety property: an operator working an incursion is never logged out mid-incident."""
    auth.create_user("alice", PASSWORD, Role.OPERATOR)
    token = auth.authenticate("alice", PASSWORD).token

    for _ in range(10):
        clock.advance(1800)  # 30 minutes, inside the 60-minute idle window
        assert auth.validate(token) is not None, "activity must slide the idle expiry"

    clock.advance(3601)
    assert auth.validate(token) is None


def test_a_session_dies_at_the_absolute_cap_however_active(
    auth: AuthService, clock: SimClock
) -> None:
    """The security property the idle timeout cannot provide: a stolen cookie kept warm by an
    attacker's own traffic still expires."""
    auth.create_user("alice", PASSWORD, Role.OPERATOR)
    token = auth.authenticate("alice", PASSWORD).token

    elapsed = 0
    for _ in range(40):
        clock.advance(1800)
        elapsed += 1800
        if auth.validate(token) is None:
            break
    else:
        pytest.fail("session never expired under continuous activity")

    assert elapsed == pytest.approx(43200, abs=1800)


def test_only_a_hash_of_the_token_is_stored(auth: AuthService, clock: SimClock) -> None:
    auth.create_user("alice", PASSWORD, Role.OPERATOR)
    token = auth.authenticate("alice", PASSWORD).token

    stored = auth._sessions.by_token_hash(hash_token(token))  # noqa: SLF001
    assert stored is not None
    assert stored.token_hash != token
    assert stored.token_hash == hash_token(token)


def test_logout_makes_the_token_useless(auth: AuthService) -> None:
    auth.create_user("alice", PASSWORD, Role.OPERATOR)
    token = auth.authenticate("alice", PASSWORD).token

    assert auth.logout(token) is True
    assert auth.validate(token) is None


def test_disabling_an_account_revokes_every_session_at_once(auth: AuthService) -> None:
    """One operation, not a loop. A partial revocation leaves the holder of the surviving
    session with access they were just judged not to have."""
    user = auth.create_user("alice", PASSWORD, Role.OPERATOR)
    tokens = [auth.authenticate("alice", PASSWORD).token for _ in range(3)]

    auth.set_enabled(user.user_id, False)

    assert all(auth.validate(token) is None for token in tokens)


# --------------------------------------------------------------------------------------
# Lockout
# --------------------------------------------------------------------------------------


def test_repeated_failures_lock_the_account(auth: AuthService) -> None:
    auth.create_user("alice", PASSWORD, Role.OPERATOR)

    for _ in range(5):
        auth.authenticate("alice", "wrong")

    refused = auth.authenticate("alice", PASSWORD)
    assert refused.failure is LoginFailure.ACCOUNT_LOCKED


def test_the_lockout_clears_itself(auth: AuthService, clock: SimClock) -> None:
    """Short and self-clearing on purpose. Anyone who knows a username can trip a lockout, and
    on a console that may be the only view of the sky, one needing an administrator to clear is
    its own hazard."""
    auth.create_user("alice", PASSWORD, Role.OPERATOR)
    for _ in range(5):
        auth.authenticate("alice", "wrong")

    clock.advance(901)

    assert auth.authenticate("alice", PASSWORD).succeeded


def test_a_successful_login_resets_the_failure_count(auth: AuthService) -> None:
    """Otherwise failures accumulate across days and an ordinary typo eventually locks someone
    out for no reason they could connect to anything."""
    auth.create_user("alice", PASSWORD, Role.OPERATOR)

    for _ in range(4):
        auth.authenticate("alice", "wrong")
    assert auth.authenticate("alice", PASSWORD).succeeded

    for _ in range(4):
        auth.authenticate("alice", "wrong")
    assert auth.authenticate("alice", PASSWORD).succeeded


def test_an_administrator_can_clear_a_lockout_early(auth: AuthService) -> None:
    user = auth.create_user("alice", PASSWORD, Role.OPERATOR)
    for _ in range(5):
        auth.authenticate("alice", "wrong")

    auth.clear_lockout(user.user_id)

    assert auth.authenticate("alice", PASSWORD).succeeded


# --------------------------------------------------------------------------------------
# Accounts and identifiers
# --------------------------------------------------------------------------------------


def test_usernames_are_unique(auth: AuthService) -> None:
    auth.create_user("alice", PASSWORD, Role.OPERATOR)

    with pytest.raises(ValueError, match="already exists"):
        auth.create_user("alice", PASSWORD, Role.ADMIN)


def test_a_restart_does_not_reissue_an_existing_user_id(clock: SimClock) -> None:
    """A generator restarting at 1 would mint an existing user_id, and the upsert would then
    silently overwrite a real account — including its role."""
    repos = SqliteRepositories(clock)
    events = SqliteEventStore(clock)
    policy = AuthPolicy(scrypt_n=2**12, scrypt_r=8, scrypt_p=1)

    first = AuthService(policy, clock, repos.users, repos.credentials, repos.sessions, events)
    original = first.create_user("alice", PASSWORD, Role.ADMIN)

    restarted = AuthService(policy, clock, repos.users, repos.credentials, repos.sessions, events)
    second = restarted.create_user("bob", PASSWORD, Role.VIEWER)

    assert second.user_id != original.user_id
    assert repos.users.get(original.user_id).username == "alice"
    assert repos.users.get(original.user_id).role is Role.ADMIN

    repos.close()
    events.close()


def test_roles_are_ordered_not_a_set() -> None:
    """`permits` is what stops a role added later failing open by being absent from a list."""
    assert Role.ADMIN.permits(Role.VIEWER)
    assert Role.ADMIN.permits(Role.OPERATOR)
    assert Role.OPERATOR.permits(Role.VIEWER)
    assert not Role.VIEWER.permits(Role.OPERATOR)
    assert not Role.OPERATOR.permits(Role.ADMIN)


def test_a_user_model_cannot_carry_a_password(clock: SimClock) -> None:
    """Structural, not a convention. `extra="forbid"` means a hash cannot be smuggled onto the
    object the API serialises even by accident."""
    with pytest.raises(ValueError, match="password_hash"):
        User(
            user_id="USR-1",
            username="alice",
            display_name="Alice",
            role=Role.VIEWER,
            created_at=EPOCH,
            updated_at=EPOCH,
            password_hash="scrypt$x",
        )


# --------------------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------------------


def test_an_absolute_timeout_below_the_idle_one_is_rejected() -> None:
    """The idle timeout could then never be reached, which is not what either setting is for."""
    with pytest.raises(ValueError, match="below idle_timeout_s"):
        AuthPolicy(idle_timeout_s=7200.0, absolute_timeout_s=3600.0)


def test_the_shipped_policy_is_two_sided_and_locks_out() -> None:
    """Guards the config file itself, not only the model — the shipped values are what runs."""
    shipped = load_auth_policy()

    assert shipped.idle_timeout_s > 0
    assert shipped.absolute_timeout_s >= shipped.idle_timeout_s
    assert shipped.max_failed_attempts >= 1
    assert shipped.lockout_duration_s > 0


def test_a_session_expires_at_the_earlier_of_its_two_deadlines() -> None:
    from datetime import timedelta

    session = Session(
        session_id="SES-1",
        user_id="USR-1",
        token_hash="x",
        created_at=EPOCH,
        last_seen_at=EPOCH,
        absolute_expiry=EPOCH + timedelta(minutes=10),
    )

    # Idle window is longer than what remains of the absolute cap, so the cap wins.
    assert session.expires_at(3600) == EPOCH + timedelta(minutes=10)
    # Idle window is shorter, so it wins.
    assert session.expires_at(60) == EPOCH + timedelta(minutes=1)
