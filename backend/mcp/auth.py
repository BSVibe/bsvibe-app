"""Bearer-token verification for the embedded MCP transport — Lift D2.

The MCP endpoint at ``/mcp`` requires ``Authorization: Bearer <jwt>``.
The JWT is the ES256 access token issued by the embedded OAuth server
(Lift D1, :mod:`backend.identity.oauth_service`).

The verification chain lives in Identity, with the issuer:
:func:`backend.identity.access_tokens.verify_access_token_with_row` checks the
JWKS signature AND the ``jti`` row behind it (revocation + the row's
``expires_at``, which is the authority on lifetime). Every surface that accepts
one of these tokens calls that one function, so the MCP transport and the REST
gate cannot drift apart on what they trust. This module only adapts the result
into an :class:`McpPrincipal`.

A failure raises :class:`McpAuthError`; the transport maps it to a 401 with the
RFC 6750 + RFC 9728 ``WWW-Authenticate`` header so MCP clients (Claude Code,
IDE plugins) can discover the authorization server via the resource-metadata
document.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.access_tokens import AccessTokenError, verify_access_token_with_row
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
        verified = await verify_access_token_with_row(token=token, issuer=issuer, session=session)
    except AccessTokenError as exc:
        raise McpAuthError("invalid_token") from exc

    return McpPrincipal(
        user_id=verified.user_id,
        workspace_id=verified.workspace_id,
        client_id=verified.client_id,
        scopes=verified.scopes,
        jti=verified.jti,
        run_id=verified.run_id,
    )


__all__ = [
    "McpAuthError",
    "resolve_principal_from_bearer",
]
