"""Bearer-token verification for the embedded MCP transport — Lift D2.

The MCP endpoint at ``/mcp`` requires ``Authorization: Bearer <jwt>``.
The JWT is the ES256 access token issued by the embedded OAuth server
(Lift D1, :mod:`backend.identity.oauth_service`).

Verification chain:

1. ``jwt.decode`` against the JWKS — proves the token was signed by
   THIS process's private key (or a key in the same rotation set).
2. Database lookup of the ``jti`` claim against
   :class:`OAuthAccessTokenRow` — proves the token has not been revoked
   (``revoked_at IS NULL``) AND has not expired beyond its DB-recorded
   ``expires_at``.

A failure at either step raises :class:`McpAuthError`; the transport
maps it to a 401 with the RFC 6750 + RFC 9728 ``WWW-Authenticate``
header so MCP clients (Claude Code, IDE plugins) can discover the
authorization server via the resource-metadata document.
"""

from __future__ import annotations

import uuid

import structlog
from jwt.exceptions import InvalidTokenError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.oauth_db import OAuthAccessTokenRow
from backend.identity.oauth_jwt import verify_access_token
from backend.mcp.api import McpPrincipal

logger = structlog.get_logger(__name__)


class McpAuthError(Exception):
    """Raised when the Bearer token fails verification."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def resolve_principal_from_bearer(
    *,
    token: str,
    issuer: str,
    session: AsyncSession,
) -> McpPrincipal:
    """Verify ``token`` and return the resolved :class:`McpPrincipal`.

    Raises :class:`McpAuthError` on any failure — the transport then
    returns 401 with the resource-metadata ``WWW-Authenticate``. We
    deliberately never surface a finer reason on the wire (RFC 6750 §3.1
    invalid_token covers all of them); the structured log records the
    underlying cause for operators.
    """
    try:
        claims = verify_access_token(token, issuer=issuer)
    except InvalidTokenError as exc:
        logger.info("mcp_auth_jwt_invalid", error=str(exc))
        raise McpAuthError("invalid_token") from exc

    try:
        jti = uuid.UUID(claims["jti"])
        user_id = uuid.UUID(claims["sub"])
        workspace_id = uuid.UUID(claims["wsp"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.info("mcp_auth_jwt_malformed_claims", error=str(exc))
        raise McpAuthError("invalid_token") from exc

    row = await session.get(OAuthAccessTokenRow, jti)
    if row is None:
        logger.info("mcp_auth_jti_not_found", jti=str(jti))
        raise McpAuthError("invalid_token")
    if row.revoked_at is not None:
        logger.info("mcp_auth_token_revoked", jti=str(jti))
        raise McpAuthError("invalid_token")

    scopes_raw = claims.get("scope") or ""
    scopes = frozenset(s for s in str(scopes_raw).split() if s)

    # T2 — a dispatched executor task's token names ONE run; the work tools bind to it and
    # refuse a token without it. A malformed claim is treated as absent (no run scope), never
    # as a different run.
    run_raw = claims.get("run_id")
    try:
        run_id = uuid.UUID(str(run_raw)) if run_raw else None
    except (ValueError, AttributeError, TypeError):
        run_id = None

    return McpPrincipal(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id=str(claims.get("client_id", "")),
        scopes=scopes,
        jti=jti,
        run_id=run_id,
    )


__all__ = [
    "McpAuthError",
    "resolve_principal_from_bearer",
]
