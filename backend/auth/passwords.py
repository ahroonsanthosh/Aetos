"""Password hashing, on the standard library.

## Why scrypt and not Argon2id

Argon2id is the first choice in most current guidance, and this is a considered departure from
it rather than an oversight.

`pyproject.toml` sets `filterwarnings = ["error::DeprecationWarning"]`, so any dependency that
emits one fails the entire test suite — and more importantly, a credential-handling library is
the worst place in a system undergoing authorisation review to take a dependency that is not
strictly necessary. `hashlib.scrypt` is in the standard library, is backed by OpenSSL, and
scrypt at adequate parameters is on OWASP's list of acceptable password hashes. The cost is
that scrypt has no data-independent-memory mode, so it is theoretically weaker than Argon2id
against side-channel attack on a shared host. Aetos runs on a dedicated site machine, which is
where that trade is worth making.

The encoded format below carries its own algorithm name, so moving to Argon2id later is a new
`_ALGORITHM` value and a branch in `verify_password`, not a password reset for every account.

## Why these parameters

Measured on the development machine before choosing:

    N=2^14  115 ms   16 MiB
    N=2^15  187 ms   32 MiB
    N=2^16  373 ms   64 MiB
    N=2^17  796 ms  128 MiB

The default is N=2^16, r=8, p=2 — an OWASP-listed equivalent-strength setting to their headline
N=2^17, r=8, p=1. The headline figure costs 128 MiB *per concurrent attempt*, which on modest
site hardware turns the login endpoint into a memory-exhaustion lever. Throttling bounds that
(see `backend/auth/service.py`), but the equivalent-strength setting removes the need to depend
on the throttle being right.

## What is deliberately absent

No pepper, and no encryption of the hash at rest. Both need a key that must live somewhere the
application can read it, which on a single-host deployment means the key sits next to the
database it protects. That is security theatre with an operational cost, and saying so is
better than shipping it and implying a guarantee that is not there.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

_ALGORITHM = "scrypt"

DEFAULT_N = 2**16
DEFAULT_R = 8
DEFAULT_P = 2
DEFAULT_DKLEN = 32
SALT_BYTES = 16

_FIELD_SEPARATOR = "$"
_EXPECTED_FIELDS = 6

MAX_PASSWORD_BYTES = 1024
"""Upper bound on what will be hashed.

scrypt's cost is set by N, r and p rather than by input length, so this is not about work
factor — it is that an unbounded field reaching a C routine is an unbounded field reaching a C
routine, and there is no legitimate password of this size.
"""


@dataclass(frozen=True, slots=True)
class ScryptParameters:
    """The cost settings a hash was produced with.

    Stored alongside every hash rather than read from configuration at verification time. A
    verifier that assumed the current settings would reject every password in the database the
    moment those settings were raised.
    """

    n: int = DEFAULT_N
    r: int = DEFAULT_R
    p: int = DEFAULT_P
    dklen: int = DEFAULT_DKLEN

    @property
    def maxmem(self) -> int:
        """Memory ceiling to hand OpenSSL.

        `hashlib.scrypt` defaults `maxmem` to 0, which OpenSSL reads as a ~32 MiB cap — below
        what these parameters need, so it raises rather than running. Computed from the
        parameters with headroom instead of hardcoded, so raising N does not silently start
        failing.
        """
        return 128 * self.n * self.r * self.p * 2


def hash_password(password: str, parameters: ScryptParameters | None = None) -> str:
    """Hash a password into the self-describing encoded form.

    A fresh random salt per call, so two accounts with the same password do not share a hash
    and a precomputed table is useless.
    """
    settings = parameters or ScryptParameters()
    salt = secrets.token_bytes(SALT_BYTES)
    return _encode(settings, salt, _derive(password, salt, settings))


def verify_password(password: str, encoded: str) -> bool:
    """Whether `password` produces `encoded`.

    Returns False rather than raising on a malformed stored hash. A corrupted or truncated
    record must fail closed as "wrong password" — raising would turn a damaged row into a 500
    that distinguishes it from a merely incorrect attempt, which is an oracle.
    """
    try:
        settings, salt, expected = _decode(encoded)
    except ValueError:
        return False

    try:
        derived = _derive(password, salt, settings)
    except (ValueError, OverflowError, MemoryError):
        return False

    return hmac.compare_digest(derived, expected)


def needs_rehash(encoded: str, parameters: ScryptParameters | None = None) -> bool:
    """Whether a stored hash was made with weaker settings than are wanted now.

    Called after a *successful* verification, which is the only moment the plaintext is
    available to re-hash with. This is what makes raising the work factor possible without
    resetting anyone's password.
    """
    wanted = parameters or ScryptParameters()
    try:
        settings, _, _ = _decode(encoded)
    except ValueError:
        # Unparseable: replace it at the next opportunity.
        return True

    return (settings.n, settings.r, settings.p, settings.dklen) < (
        wanted.n,
        wanted.r,
        wanted.p,
        wanted.dklen,
    )


def dummy_verify(parameters: ScryptParameters | None = None) -> None:
    """Do the work of a verification without having a user to verify.

    Called when the username does not exist. Without it, an unknown username returns in
    microseconds and a known one takes the full hashing time, which tells an attacker which
    usernames are real — and a list of real operator usernames is most of the way to a targeted
    lockout or a credential-stuffing run.
    """
    settings = parameters or ScryptParameters()
    _derive("", secrets.token_bytes(SALT_BYTES), settings)


def _derive(password: str, salt: bytes, settings: ScryptParameters) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password exceeds {MAX_PASSWORD_BYTES} bytes")

    return hashlib.scrypt(
        encoded,
        salt=salt,
        n=settings.n,
        r=settings.r,
        p=settings.p,
        dklen=settings.dklen,
        maxmem=settings.maxmem,
    )


def _encode(settings: ScryptParameters, salt: bytes, derived: bytes) -> str:
    return _FIELD_SEPARATOR.join(
        (
            _ALGORITHM,
            str(settings.n),
            str(settings.r),
            str(settings.p),
            _b64(salt),
            _b64(derived),
        )
    )


def _decode(encoded: str) -> tuple[ScryptParameters, bytes, bytes]:
    parts = encoded.split(_FIELD_SEPARATOR)
    if len(parts) != _EXPECTED_FIELDS or parts[0] != _ALGORITHM:
        raise ValueError("unrecognised password hash format")

    _, raw_n, raw_r, raw_p, raw_salt, raw_hash = parts
    try:
        salt = _unb64(raw_salt)
        derived = _unb64(raw_hash)
        settings = ScryptParameters(n=int(raw_n), r=int(raw_r), p=int(raw_p), dklen=len(derived))
    except Exception as exc:  # malformed field of any kind
        raise ValueError("malformed password hash") from exc

    if settings.n < 2 or settings.n & (settings.n - 1):
        raise ValueError("scrypt N must be a power of two above one")

    return settings, salt, derived


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)
