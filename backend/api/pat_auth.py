"""Auth resolver for the PAT endpoints — accepts either credential class.

A PAT exists so a browserless client can reach `/mcp`. Minting one from such a
host therefore cannot require the PWA's Supabase session JWT — the CLI holds an
ES256 access token issued by our own embedded OAuth server (``bsvibe login``,
including its ``--manual`` out-of-band flow). Both have to work.

**How the class is chosen.** By the token's ``iss`` claim, read *unverified* and
used only to pick which verifier runs — never to trust anything. This is a
single deterministic branch, NOT a try-A-then-try-B fallback: sequential
attempts blur the failure (an expired session JWT gets reported as a bad access
token), cost a pointless database round-trip on every auth failure, and violate
the no-implicit-routing rule. Compare
:func:`backend.api.v1.workers_register_auth.resolve_workspace_for_bearer`, which
does fall back; that pattern is deliberately not copied here.

**Why an access token needs ``mcp:admin`` to mint.** A credential that can mint
credentials is an escalation path, and the scopes already separate cleanly:

* ``bsvibe login`` registers a client asking for ``mcp:read mcp:write
  mcp:admin`` — a human at a browser approved it, so it may mint.
* :func:`backend.identity.oauth_service.issue_run_task_token` grants
  ``mcp:read mcp:write`` to a *dispatched executor task*. That agent must never
  be able to turn its 90-minute task credential into a permanent one.

The ``run_id`` check is belt-and-braces on the same rule: it holds even if a
future change widens run-token scope.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

import jwt
import structlog
from fastapi import Depends, Header, HTTPException, status
from fastapi.security.utils import get_authorization_scheme_param
from jwt.exceptions import InvalidTokenError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session
from backend.config import get_settings
from backend.data.rls import set_workspace_guc
from backend.data.scoping import set_current_workspace_id
from backend.identity.service import get_user_by_supabase_id, resolve_workspace_id
from backend.shared.authz.auth import AuthError, parse_user_token, verify_user_jwt
from backend.shared.authz.settings import get_settings as get_authz_settings

logger = structlog.get_logger(__name__)

#: Scope an access token must carry to mint or revoke a PAT.
PAT_ADMIN_SCOPE = "mcp:admin"


@dataclass(frozen=True)
class PatPrincipal:
    """Who is managing personal access tokens, and in which workspace."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    auth_kind: str  # "session_jwt" | "mcp_access_token"


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing Authorization header"
        )
    scheme, token = get_authorization_scheme_param(authorization)
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid Authorization scheme"
        )
    return token


def _unverified_issuer(token: str) -> str | None:
    """The ``iss`` claim without verifying anything — a routing hint only."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except InvalidTokenError:
        return None
    issuer = claims.get("iss")
    return str(issuer) if isinstance(issuer, str) and issuer else None


async def _from_access_token(token: str, session: AsyncSession) -> PatPrincipal:
    """Our own ES256 access token — the credential ``bsvibe login`` stores.

    The verifier is imported lazily: ``backend.mcp`` pulls in the tool package,
    which imports ``backend.api.v1``, which imports this module's caller. At
    call time the cycle is long since resolved.
    """
    from backend.mcp.auth import (  # noqa: PLC0415 — breaks an import cycle
        McpAuthError,
        resolve_principal_from_bearer,
    )

    issuer = get_settings().oauth_issuer
    try:
        principal = await resolve_principal_from_bearer(token=token, issuer=issuer, session=session)
    except McpAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid access token"
        ) from exc

    if principal.run_id is not None:
        logger.info("pat_auth_run_scoped_token_rejected", run_id=str(principal.run_id))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="a run-scoped task token cannot manage personal access tokens",
        )
    if not principal.has_scope(PAT_ADMIN_SCOPE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"managing personal access tokens requires the {PAT_ADMIN_SCOPE} scope",
        )
    return PatPrincipal(
        user_id=principal.user_id,
        workspace_id=principal.workspace_id,
        auth_kind="mcp_access_token",
    )


async def _from_session_jwt(token: str, session: AsyncSession) -> PatPrincipal:
    """The PWA's Supabase / BSVibe-Auth session JWT."""
    try:
        payload = verify_user_jwt(token, get_authz_settings())
        user = parse_user_token(payload)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    row = await get_user_by_supabase_id(session, user.id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="no user record for principal"
        )
    workspace_id = await resolve_workspace_id(session, supabase_user_id=user.id)
    if workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="no workspace membership for principal"
        )
    return PatPrincipal(user_id=row.id, workspace_id=workspace_id, auth_kind="session_jwt")


async def resolve_pat_principal(
    authorization: Annotated[str | None, Header()] = None,
    session: Annotated[AsyncSession, Depends(get_db_session)] = ...,  # type: ignore[assignment]
) -> PatPrincipal:
    """Resolve the caller of a PAT endpoint from either credential class."""
    token = _bearer(authorization)
    if _unverified_issuer(token) == get_settings().oauth_issuer:
        principal = await _from_access_token(token, session)
    else:
        principal = await _from_session_jwt(token, session)

    # Same publication `get_workspace_id` performs: the ORM auto-filter reads
    # the contextvar and Postgres RLS reads the GUC. Skipping either would make
    # these routes the one place where workspace isolation is advisory.
    set_current_workspace_id(principal.workspace_id)
    await set_workspace_guc(await session.connection(), principal.workspace_id)
    return principal


__all__ = ["PAT_ADMIN_SCOPE", "PatPrincipal", "resolve_pat_principal"]
