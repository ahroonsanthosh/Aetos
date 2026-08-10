"""Authentication and authorisation.

See `README.md` in this directory for the model and its limits, and ADR 0016 for why it is
built this way. `docs/failure_modes.md` FM-001 records the state this module exists to end.
"""

from backend.auth.models import Credential, Role, Session, User
from backend.auth.policy import SESSION_COOKIE_NAME, AuthPolicy, load_auth_policy, policy_path
from backend.auth.service import (
    AuthenticatedOperator,
    AuthService,
    LoginFailure,
    LoginResult,
    hash_token,
)

__all__ = [
    "SESSION_COOKIE_NAME",
    "AuthPolicy",
    "AuthService",
    "AuthenticatedOperator",
    "Credential",
    "LoginFailure",
    "LoginResult",
    "Role",
    "Session",
    "User",
    "hash_token",
    "load_auth_policy",
    "policy_path",
]
