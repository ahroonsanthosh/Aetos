# ADR 0016 — Authentication and authorisation

**Status:** Accepted
**Date:** 2026-08-10
**Supersedes:** [ADR 0008](0008-determinism-backpressure-auth.md) §3, which deferred this
deliberately. §1 (determinism) and §2 (backpressure) of that record stand unchanged.

## Context

ADR 0008 §3 argued that a half-built authentication system is worse than none, because it looks
like security: it invites the system to be exposed on the assumption that something is
checking, when nothing is. That reasoning was right and is why this was not built earlier.

What changed is the deployment. Aetos is going to a site rather than staying on a laptop, and
FM-001's mitigation — "must not be exposed to an untrusted network" — is not a mitigation once
that stops being true. No test suite closes FM-001; only an access-control decision does.

The specific gap: `OPERATOR_ACKNOWLEDGED` events name an operator the system never verified, so
the log cannot support any claim about *who* did anything. That is the first question an
authorisation review asks of an audit trail.

## Decision

### 1. Session cookies, not bearer tokens

Forced by the medium rather than chosen on taste. `/api/v1/stream` carries the entire live
picture and must be authenticated, and a browser cannot set headers on a WebSocket upgrade. The
alternatives are a token in the query string — which lands in access logs, browser history and
referer headers — or an unauthenticated stream, which would make the REST gate decorative.

Cookies are `HttpOnly` (script cannot read them), `SameSite=Strict` (a cross-site request never
carries one), and `Secure` on any TLS connection. They are sent automatically on a same-origin
upgrade, so the stream needs no client-side token handling at all.

**No CSRF token.** `SameSite=Strict` plus same-origin-by-construction — `backend/api/static.py`
serves the bundle from this process — covers the state-changing endpoints. A double-submit
token would add a second thing to misconfigure for no additional guarantee *here*. If a
cross-origin client ever exists, that trade changes and a token becomes necessary.

### 2. Three roles, ordered

`viewer` reads, `operator` also acknowledges alerts, `admin` also manages accounts. The
smallest set that allows giving someone situational awareness without letting them acknowledge,
which is the distinction a reviewer asks about.

Checks use an ordering (`Role.permits`) rather than set membership. An enumeration of qualifying
roles silently omits any role added later, and it fails *open* for the new one.

### 3. Gating is fail-closed, and tested as such

The baseline gate is a **router-level** dependency, not a per-handler decorator, so a route
added later is gated by default and has to opt out visibly.

`tests/integration/test_auth_api.py` walks the application's own route table and makes an
unauthenticated request to every route, asserting 401 unless the route is on a short named
public list. The failure being designed against is not a wrong role on an endpoint someone
considered — it is an endpoint nobody considered at all.

401 and 403 stay distinct: no session versus a session without the rank. Sending a viewer to the
login page for a permissions problem would make it look like a broken session.

### 4. Passwords: stdlib scrypt, self-describing

`hashlib.scrypt` rather than argon2-cffi. `pyproject.toml` sets
`filterwarnings = ["error::DeprecationWarning"]`, so a noisy dependency fails the whole suite —
and a credential-handling library is the worst place to take an unnecessary supply-chain
dependency in a system heading for review. scrypt at adequate parameters is on OWASP's accepted
list. The cost is no data-independent-memory mode, so it is theoretically weaker than Argon2id
against side-channel attack on a shared host; Aetos runs on a dedicated site machine.

Default N=2¹⁶, r=8, p=2 — an OWASP-listed equivalent to their headline N=2¹⁷, r=8, p=1, which
costs 128 MiB *per concurrent attempt* and would make the login endpoint a memory-exhaustion
lever. Measured here at 796 ms and 373 ms respectively.

Hashes carry their own parameters, so the work factor can be raised later and each account
re-hashed on its next successful login rather than everyone being reset.

### 5. Sessions expire two ways

Sliding idle timeout (60 min default) **and** an absolute cap (12 h). Both are needed and
neither alone is enough.

The idle timeout is a safety property, not a convenience: an operator forced to re-authenticate
while tracking a contact is an operator not tracking a contact, so activity slides it out
indefinitely. The absolute cap is the security half: a stolen cookie kept warm by an attacker's
own traffic would otherwise never expire.

Only a SHA-256 of the token is stored. A database read — a backup, a stolen file, a support
dump — yields no live session. Plain digest rather than a password hash, because the token is
256 bits of CSPRNG output and stretching would only slow every authenticated request.

The account is re-read on every request rather than cached on the session, so a role revoked or
an account disabled mid-shift takes effect on the next request rather than at the next login.

### 6. Failures are indistinguishable, in message and in timing

Wrong password, unknown username, disabled account and locked-out account all return the same
response. The log records which; the client is told only that it failed. An unknown username
still pays the cost of a password hash, so timing does not distinguish them either — measured at
392 ms against 397 ms.

The lockout case itches, because "you are locked out for 15 minutes" is genuinely more useful.
It is also a free oracle for whether a username exists, so it stays in the log where an
administrator can see it and an attacker cannot.

Lockout is short (15 min) and self-clearing. It is a denial-of-service lever pointed at a
legitimate operator — anyone who knows a username can trip it — and on a console that may be the
only view of the sky, a lockout outliving the attack is its own hazard.

### 7. The deployment posture is enforced, not documented

`guard_bind` refuses to start when the configuration would expose the system: no authentication
off loopback, no accounts off loopback, or off loopback without `--behind-proxy`.

Aetos does not terminate TLS and holds no private key. `--behind-proxy` is an explicit assertion
by whoever starts the process that TLS is terminated in front of it, and it is also what enables
trusting `X-Forwarded-Proto` — never inferred from the header alone, because headers are
supplied by whoever is connecting.

A guard rather than a warning, because documentation does not run.

### 8. No default credential

The first account is created by `python -m backend.auth.admin`, prompting for the password
rather than taking it as an argument — an argument lands in shell history and the process table.
No shipped default, no generated first-run password printed to a terminal.

## Consequences

**Good.** The event log can answer *who*. `Identity.authenticated` is true and
`OperatorContext.basis` reads `session`, and both fields existed already for exactly this. The
two eras of the log are distinguishable rather than silently merged. FM-001 closes.

**Costs.** Login takes ~500 ms, which is the work factor doing its job. The unauthenticated
health response is narrower than it was, which makes some monitoring setups slightly less
informative. `LocalOperatorIdentityProvider` still exists for single-user machines and tests, so
there remains a way to run without authentication — bounded by the startup guard rather than by
convention.

**Not addressed.** No password reset flow, no multi-factor, no account lockout notification, no
session listing for administrators. All are reasonable next steps and none is required to stop
an unauthenticated console being reachable from a network, which is what this record is for.

**Also unchanged.** Nothing here decides a response or reaches anything that could act. Roles
govern who may *look* and who may *acknowledge*; the most severe output the system produces
remains a recommendation to a human.

## Amendment — a second exposure assertion, for containers

Added when `Dockerfile`/`docker-compose.yml` were reconciled with this record after the fact —
they were written before authentication existed and predate `guard_bind` entirely, so nothing
had yet checked them against it.

A container has to bind `0.0.0.0`: container port publishing cannot reach a socket bound to
`127.0.0.1` inside the network namespace, which is a property of how containers are networked,
not a statement about who can reach the port. `docker-compose.yml` publishes that port to
`127.0.0.1` on the host, so the actual exposure is loopback, same as running the process
directly — but `guard_bind` saw only the bind host, which is `0.0.0.0`, and refused to start.

Reusing `--behind-proxy` to satisfy the guard was considered and rejected: it also enables
trusting `X-Forwarded-Proto`, and nothing is proxying, so that would mark the session cookie
`Secure` on the strength of a header nobody is actually setting — trading one gap for another
instead of closing it.

`--published-loopback-only` is a second, distinct exposure assertion for exactly this shape:
non-loopback bind, loopback-only publish, no proxy. It satisfies the same refusal `--behind-
proxy` does, and nothing else — it does not touch `X-Forwarded-Proto` trust, so the cookie
stays non-`Secure` under it, which is correct: nothing terminated TLS. The authentication and
account-count refusals are unaffected either way; a container still needs a real login and a
real account, same as anything else off loopback. See `backend/main.py::guard_bind` and
`docs/deployment.md`.
