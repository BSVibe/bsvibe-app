"""Auth resolver for ``POST /api/v1/workers/register`` — Lift E4.

The Lift E4 design retires the install-token paste step in favour of the
GitHub-Actions-runner UX::

    $ bsvibe-worker register --name mac-mini

The CLI sends ``Authorization: Bearer <token>`` where the bearer is either:

* A Supabase session JWT (the same one the PWA uses). The CLI obtains it via
  ``bsvibe login`` (PKCE loopback / device flow against ``auth.bsvibe.dev``)
  or by reading ``~/.config/bsvibe/credentials.json`` produced by that flow.
* An ES256 MCP access token issued by the embedded OAuth server (Lift D1).
  Useful in CI / scripted contexts where a PAT-style token is preferred.

The endpoint derives ``workspace_id`` from the verified bearer — the body
never carries a ``workspace_id`` (so a client cannot mint a worker in someone
else's workspace by guessing IDs).

For backward compatibility the legacy ``X-Install-Token`` path is preserved
through Lift E5; it lives in the route handler itself rather than this
resolver because its workspace derivation is structurally different
(token → DB lookup, not bearer → JWT claims).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from fastapi.security.utils import get_authorization_scheme_param
from jwt.exceptions import InvalidTokenError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.identity.oauth_db import OAuthAccessTokenRow
from backend.identity.oauth_jwt import verify_access_token
from backend.identity.service import resolve_workspace_id
from backend.shared.authz.auth import AuthError, parse_user_token, verify_user_jwt
from backend.shared.authz.settings import get_settings as get_authz_settings

logger = structlog.get_logger(__name__)


class BearerAuthError(Exception):
    """Raised when a bearer-token register attempt fails."""


@dataclass(frozen=True)
class ResolvedRegisterPrincipal:
    """The workspace + actor a register request operates within."""

    workspace_id: uuid.UUID
    auth_kind: str  # "supabase_jwt" | "mcp_access_token"


def extract_bearer(authorization: str | None) -> str | None:
    """Return the raw bearer token from an ``Authorization`` header, or ``None``.

    Unlike :func:`backend.shared.authz.deps._extract_bearer` this never
    raises — the register endpoint must distinguish "no bearer at all" (try
    the legacy install-token path) from "bearer present but invalid"
    (401). Caller decides.
    """
    if not authorization:
        return None
    scheme, token = get_authorization_scheme_param(authorization)
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def _try_mcp_access_token(
    bearer: str, session: AsyncSession, errors: list[str]
) -> ResolvedRegisterPrincipal | None:
    """Verify ``bearer`` as an ES256 MCP access token. ``None`` on shape miss."""
    try:
        claims = verify_access_token(bearer, issuer=get_settings().oauth_issuer)
    except InvalidTokenError as exc:
        errors.append(f"mcp_token: {exc}")
        return None
    try:
        jti = uuid.UUID(claims["jti"])
        workspace_id = uuid.UUID(claims["wsp"])
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"mcp_token_claims: {exc}")
        return None
    row = await session.get(OAuthAccessTokenRow, jti)
    if row is None:
        errors.append("mcp_token: jti not found")
        return None
    if row.revoked_at is not None:
        errors.append("mcp_token: revoked")
        return None
    scopes_raw = claims.get("scope") or ""
    scopes = frozenset(s for s in str(scopes_raw).split() if s)
    # Register is a write operation — require mcp:write for MCP tokens.
    if "mcp:write" not in scopes:
        errors.append("mcp_token: missing mcp:write scope")
        return None
    logger.info(
        "worker_register_auth_mcp_token",
        workspace_id=str(workspace_id),
        jti=str(jti),
    )
    return ResolvedRegisterPrincipal(workspace_id=workspace_id, auth_kind="mcp_access_token")


async def _try_supabase_jwt(
    bearer: str, session: AsyncSession, errors: list[str]
) -> ResolvedRegisterPrincipal | None:
    """Verify ``bearer`` as a Supabase session JWT. ``None`` on shape miss."""
    try:
        payload = verify_user_jwt(bearer, get_authz_settings())
    except AuthError as exc:
        errors.append(f"supabase_jwt: {exc}")
        return None
    try:
        user = parse_user_token(payload)
    except AuthError as exc:
        errors.append(f"supabase_jwt_user: {exc}")
        return None
    workspace_id = await resolve_workspace_id(session, supabase_user_id=user.id)
    if workspace_id is None:
        errors.append("supabase_jwt: no workspace membership")
        return None
    logger.info(
        "worker_register_auth_supabase_jwt",
        workspace_id=str(workspace_id),
        user_id=user.id,
    )
    return ResolvedRegisterPrincipal(workspace_id=workspace_id, auth_kind="supabase_jwt")


async def resolve_workspace_for_bearer(
    bearer: str, session: AsyncSession
) -> ResolvedRegisterPrincipal:
    """Verify ``bearer`` and return the workspace + auth kind.

    Tries the MCP access-token shape first (it has stricter shape
    requirements: an ``kid`` header and an ES256 signature against the
    embedded OAuth JWKS), then falls back to the Supabase session JWT.
    Either path can be valid — but neither raises on shape mismatch, only on
    verified-but-rejected (e.g. revoked MCP token, no Supabase membership).
    Any path-level failure is collected and surfaced as
    :class:`BearerAuthError` only when ALL paths fail.
    """
    errors: list[str] = []
    resolved = await _try_mcp_access_token(bearer, session, errors)
    if resolved is not None:
        return resolved
    resolved = await _try_supabase_jwt(bearer, session, errors)
    if resolved is not None:
        return resolved
    logger.info("worker_register_auth_failed", reasons=errors)
    raise BearerAuthError("; ".join(errors) or "invalid bearer token")


__all__ = [
    "BearerAuthError",
    "ResolvedRegisterPrincipal",
    "extract_bearer",
    "resolve_workspace_for_bearer",
]
