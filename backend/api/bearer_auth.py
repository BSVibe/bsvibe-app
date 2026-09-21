"""One trust set for every bearer the REST surface accepts (#1017).

BSVibe issues **two classes of user credential** and both are legitimate:

* the **session JWT** the PWA holds (Supabase / BSVibe-Auth, verified against
  ``USER_JWT_JWKS_URL``), whose ``sub`` is a ``supabase_user_id``;
* the **ES256 access token our own embedded OAuth server mints** (Lift D1),
  which is what ``bsvibe login`` writes to ``~/.config/bsvibe/credentials.json``
  and what a PAT is. Its ``sub`` is a ``UserRow.id`` and it carries its own
  ``wsp`` (workspace) and ``scope`` claims.

Before this module the v1 gate verified only the first class, so every ordinary
CLI command 401'd in production — *"JWKS resolution failed: unable to find a
signing key that matches"* — while the suite stayed green, because every test
signs its own session JWT. The workaround had been to mount one router
(``/api/v1/oauth/pats``) OUTSIDE the gate. One escaped; the rest of v1 did not.

Pointing ``USER_JWT_JWKS_URL`` at our issuer is NOT the fix: it breaks the PWA,
and the CLI still fails downstream because its ``sub`` is not a
``supabase_user_id``. Two issuers do not fit in one config slot — the gate has
to know both.

**How the class is chosen.** By the token's ``iss`` claim, read *unverified*
and used only to pick which verifier runs — never to trust anything. A single
deterministic branch, NOT try-A-then-try-B: sequential attempts blur the
failure (an expired session JWT gets reported as a bad access token), cost a
pointless database round-trip on every auth failure, and violate the
no-implicit-routing rule.

**Why an access token is held to more than a signature.** It is a
bearer credential that lives on disk for weeks, so the row behind it is the
authority (revocation, and an ``expires_at`` that must be shortenable without
reissuing the JWT) and three further rules apply at this gate:

* a **run-scoped** token (:func:`~backend.identity.oauth_service.issue_run_task_token`)
  is a dispatched executor's 90-minute credential for ONE run's worktree. It
  reaches the work tools over ``/mcp`` and nothing else — if it also opened the
  REST API, a leaked task token would be a workspace-wide foothold;
* **scope is enforced by HTTP method**: reads need ``mcp:read``, writes need
  ``mcp:write``. A read-only PAT that could POST would make the scopes
  decorative;
* the workspace is the token's **own ``wsp`` claim**, not the caller's first
  membership — a token NAMES its tenant, and the founder really does hold
  memberships in more than one workspace. The membership is still checked, so
  losing access does not wait for the token's expiry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt
import structlog
from fastapi import HTTPException, status
from fastapi.security.utils import get_authorization_scheme_param
from jwt.exceptions import InvalidTokenError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.identity.access_tokens import AccessTokenError, verify_access_token_with_row
from backend.identity.db import UserRow
from backend.identity.infrastructure.repositories.membership_repository_sql import (
    SqlAlchemyMembershipRepository,
)
from backend.shared.authz.auth import AuthError, parse_user_token, verify_user_jwt
from backend.shared.authz.settings import get_settings as get_authz_settings
from backend.shared.authz.types import User

logger = structlog.get_logger(__name__)

#: Scope an access token must carry to read through the REST surface.
READ_SCOPE = "mcp:read"
#: ...and to change anything through it.
WRITE_SCOPE = "mcp:write"

#: HTTP methods that only read. Everything else is held to :data:`WRITE_SCOPE`.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

SESSION_JWT = "session_jwt"
OAUTH_ACCESS_TOKEN = "oauth_access_token"  # noqa: S105 — an auth-kind label, not a secret


@dataclass(frozen=True)
class ApiPrincipal:
    """The verified caller of a REST request, whichever credential they used.

    ``workspace_id`` / ``user_row_id`` are populated only for the access-token
    class, where the token itself names the tenant. For a session JWT they stay
    ``None`` and the workspace is resolved downstream from the caller's active
    membership — deliberately lazily, so routes that need only the identity
    (e.g. "list the workspaces I belong to") keep working for a principal that
    has no membership yet.
    """

    user: User
    auth_kind: str
    workspace_id: uuid.UUID | None = None
    user_row_id: uuid.UUID | None = None
    scopes: frozenset[str] = frozenset()


def extract_bearer(authorization: str | None) -> str:
    """Return the raw bearer token, or raise the 401 the gate owes the caller."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
        )
    scheme, token = get_authorization_scheme_param(authorization)
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid Authorization scheme",
        )
    return token


def unverified_issuer(token: str) -> str | None:
    """The ``iss`` claim without verifying anything — a routing hint only."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except InvalidTokenError:
        return None
    issuer = claims.get("iss")
    return str(issuer) if isinstance(issuer, str) and issuer else None


def is_our_access_token(token: str) -> bool:
    """True when ``token`` claims to come from our embedded OAuth issuer."""
    return unverified_issuer(token) == get_settings().oauth_issuer


def required_scope_for(method: str) -> str:
    """The scope an access token needs to make a ``method`` request."""
    return READ_SCOPE if method.upper() in _READ_METHODS else WRITE_SCOPE


async def resolve_session_jwt(token: str) -> ApiPrincipal:
    """Verify the PWA's session JWT. Identity only — no database round-trip."""
    try:
        payload = verify_user_jwt(token, get_authz_settings())
        user = parse_user_token(payload)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return ApiPrincipal(user=user, auth_kind=SESSION_JWT)


async def resolve_access_token(token: str, *, method: str, session: AsyncSession) -> ApiPrincipal:
    """Verify our own ES256 access token and apply this gate's extra rules."""
    try:
        verified = await verify_access_token_with_row(
            token=token, issuer=get_settings().oauth_issuer, session=session
        )
    except AccessTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid access token"
        ) from exc

    if verified.run_id is not None:
        logger.info("api_auth_run_scoped_token_rejected", run_id=str(verified.run_id))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="a run-scoped task token cannot reach the REST API",
        )

    needed = required_scope_for(method)
    if not verified.has_scope(needed):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"this request requires the {needed} scope",
        )

    user_row = await session.get(UserRow, verified.user_id)
    if user_row is None:
        logger.info("api_auth_token_user_missing", user_id=str(verified.user_id))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="no user record for principal"
        )

    membership = await SqlAlchemyMembershipRepository(session).active_for_user_in_workspace(
        verified.user_id, verified.workspace_id
    )
    if membership is None:
        logger.info(
            "api_auth_token_membership_missing",
            user_id=str(verified.user_id),
            workspace_id=str(verified.workspace_id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no workspace membership for principal",
        )

    return ApiPrincipal(
        user=User(id=user_row.supabase_user_id, email=user_row.email),
        auth_kind=OAUTH_ACCESS_TOKEN,
        workspace_id=verified.workspace_id,
        user_row_id=user_row.id,
        scopes=verified.scopes,
    )


async def resolve_api_principal(token: str, *, method: str, session: AsyncSession) -> ApiPrincipal:
    """Resolve ``token`` as whichever credential class its ``iss`` claims."""
    if is_our_access_token(token):
        return await resolve_access_token(token, method=method, session=session)
    return await resolve_session_jwt(token)


__all__ = [
    "OAUTH_ACCESS_TOKEN",
    "READ_SCOPE",
    "SESSION_JWT",
    "WRITE_SCOPE",
    "ApiPrincipal",
    "extract_bearer",
    "is_our_access_token",
    "required_scope_for",
    "resolve_access_token",
    "resolve_api_principal",
    "resolve_session_jwt",
    "unverified_issuer",
]
