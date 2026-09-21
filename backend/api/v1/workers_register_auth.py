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

Lift E5 (2026-06-06) — this is now the ONLY register auth path. The legacy
``X-Install-Token`` header and its DB-backed install-token system are gone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from fastapi.security.utils import get_authorization_scheme_param
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.identity.access_tokens import AccessTokenError, verify_access_token_with_row
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
    raises — the register endpoint must distinguish "no bearer at all"
    (401 "missing Authorization bearer") from "bearer present but invalid"
    (401 "invalid bearer token"). Caller decides.
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
    """Verify ``bearer`` as an ES256 MCP access token. ``None`` on shape miss.

    Verification is :func:`~backend.identity.access_tokens.verify_access_token_with_row`,
    the same function the MCP transport and the v1 gate call. This path used to
    roll its own chain and had drifted: it checked ``revoked_at`` but never the
    row's ``expires_at``, so a token whose lifetime had been shortened in the
    database could still register a worker (#1017).
    """
    try:
        verified = await verify_access_token_with_row(
            token=bearer, issuer=get_settings().oauth_issuer, session=session
        )
    except AccessTokenError as exc:
        errors.append(f"mcp_token: {exc.reason}")
        return None
    # Register is a write operation — require mcp:write for MCP tokens.
    if not verified.has_scope("mcp:write"):
        errors.append("mcp_token: missing mcp:write scope")
        return None
    logger.info(
        "worker_register_auth_mcp_token",
        workspace_id=str(verified.workspace_id),
        jti=str(verified.jti),
    )
    return ResolvedRegisterPrincipal(
        workspace_id=verified.workspace_id, auth_kind="mcp_access_token"
    )


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
