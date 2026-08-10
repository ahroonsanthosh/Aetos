# Authentication and authorisation

Who someone is, what they may do, and how long that lasts. Closes FM-001, which was the one
thing standing between this system and a network. See ADR 0016 for the decisions; this file is
about reading the code.

## Three types, and why not one

| Type | Holds | Safe to serialise |
| --- | --- | --- |
| `User` | identity, role, enabled, lockout state | yes |
| `Credential` | the password verifier | **never leaves this module** |
| `Session` | a proved claim, with two expiries | hash only |

The storage convention in this codebase puts a model's JSON in a `document` column and
re-validates it on read. A password hash stored as a `User` field would therefore round-trip
through the very object the API serialises, and one careless response model would leak every
credential at once. Separate types and separate tables make that impossible rather than
forbidden.

`CredentialRepository` deliberately has no `list_all`. There is no legitimate reason to
enumerate every hash in the system, and an interface that offers it is one someone will
eventually use.

## Passwords

`hashlib.scrypt`, from the standard library. Default N=2¹⁶, r=8, p=2 — an OWASP-listed
equivalent to their headline N=2¹⁷/r=8/p=1, chosen because the headline costs 128 MiB per
concurrent attempt and would make the login endpoint a memory-exhaustion lever.

Hashes are **self-describing**: `scrypt$N$r$p$salt$hash`. That is what makes the work factor
raisable later without resetting anyone's password — `needs_rehash` is checked after every
successful verification, which is the only moment the plaintext is available to re-hash with.

`verify_password` returns False on a malformed stored hash rather than raising. A corrupted row
must fail closed as "wrong password"; raising would distinguish a damaged record from an
incorrect attempt, which is an oracle.

## Sessions

Two expiries, and both are needed:

- **Idle (60 min, sliding)** — a *safety* property. An operator forced to re-authenticate while
  tracking a contact is an operator not tracking a contact, so any activity pushes it out.
- **Absolute (12 h, fixed at login)** — the security half. A stolen cookie kept warm by an
  attacker's own traffic would otherwise never expire.

Only `sha256(token)` is stored. A database read — a backup, a stolen file, a support dump —
yields no live session. A plain digest rather than a password hash, because the token is 256
bits of CSPRNG output: guessing is not the threat, and stretching would slow every
authenticated request for nothing.

The account is re-read from the database on **every** request rather than cached on the
session, so a role revoked or an account disabled mid-shift bites on the next request instead
of at the next login.

Session IDs are random where every other identifier here is sequential. Sessions are the one
kind of row genuinely deleted, so a counter would eventually hand a fresh session the ID of one
already named in the log — and two logins sharing a `session_id` makes the audit trail
ambiguous exactly where it must be precise.

## Failing without saying why

Wrong password, unknown username, disabled account and locked-out account produce the **same**
response. The log records which; the caller learns only that it failed.

Timing says nothing either: an unknown username still pays for a full password hash
(`dummy_verify`). Measured at 392 ms against 397 ms, inside the run-to-run spread, and pinned by
a test that would fail decisively if the dummy hash were removed, because the gap would then be
the entire work factor.

The lockout case is the one that itches — "locked out for 15 minutes" is genuinely more useful
to a real operator. It is also a free oracle for whether a username exists, so it stays in the
log where an administrator can see it and an attacker cannot.

Lockout is short and self-clearing. It is a denial-of-service lever pointed at a legitimate
operator, since anyone who knows a username can trip it, and on a console that may be the only
view of the sky, a lockout outliving the attack is its own hazard.

## Everything uses the injected `Clock`

`tests/unit/test_module_boundaries.py` AST-scans for `datetime.now`, `time.time` and
`time.monotonic` and fails the build if they appear outside `backend/common/clock.py`. Every
deadline here — session expiry, lockout windows, token issue times — comes from the clock.

That constraint pays for itself: session expiry is tested by advancing a `SimClock`, not by
sleeping, so the 12-hour absolute cap is exercised in milliseconds.

## What this module does not do

It does not decide who may authorise a physical action. Knowing which account made a request is
a precondition for that question, not an answer to it — see `response/README.md`.

There is no password reset flow, no multi-factor, and no administrator view of active sessions.
An administrator resets a password with `python -m backend.auth.admin passwd`, then `disable`
and `enable` to end that account's existing sessions.
