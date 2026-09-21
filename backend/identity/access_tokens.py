"""Verify an access token this backend issued — signature AND the row behind it.

The embedded OAuth server (Lift D1) is the issuer, so verification belongs to
Identity. Every surface that accepts one of its tokens — the MCP transport, the
PAT endpoints, worker register, and (since #1017) the whole v1 REST gate —
calls THIS function, so they share one trust set. When each surface rolled its
own chain they drifted: ``workers_register_auth`` checked ``revoked_at`` but
never the row's ``expires_at``, so a token whose lifetime had been shortened in
the database could still register a worker.

Two steps, and the second is not redundant:

1. ``jwt.decode`` against the JWKS — proves the token was signed by THIS
   process's private key (or a key in the same rotation set).
2. The ``jti`` row lookup — proves the token has not been revoked
   (``revoked_at IS NULL``) and has not passed the row's ``expires_at``. The
   row is the authority on lifetime: a PAT carries no ``exp`` claim at all, and
   shortening any token's life has to take effect without reissuing the JWT.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from jwt.exceptions import InvalidTokenError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.oauth_db import OAuthAccessTokenRow, aware_utc
from backend.identity.oauth_jwt import verify_access_token

logger = structlog.get_logger(__name__)


class AccessTokenError(Exception):
    """The bearer is not a currently-valid access token from this issuer."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedAccessToken:
    """The claims of an access token that passed both verification steps."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    client_id: str
    scopes: frozenset[str]
    jti: uuid.UUID
    #: The ExecutionRun this token may act on — set ONLY on the short-lived token a
    #: dispatched executor task carries. ``None`` on every ordinary token (the
    #: founder's editor, the CLI, a PAT). Surfaces that are not the work tools
    #: refuse a token that carries it.
    run_id: uuid.UUID | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


async def verify_access_token_with_row(
    *, token: str, issuer: str, session: AsyncSession
) -> VerifiedAccessToken:
    """Verify ``token`` end to end. Raises :class:`AccessTokenError` on any failure.

    The reason is deliberately coarse on the wire (RFC 6750 §3.1
    ``invalid_token`` covers all of them); the structured log records the
    underlying cause for operators.
    """
    try:
        claims = verify_access_token(token, issuer=issuer)
    except InvalidTokenError as exc:
        logger.info("access_token_jwt_invalid", error=str(exc))
        raise AccessTokenError("invalid_token") from exc

    try:
        jti = uuid.UUID(claims["jti"])
        user_id = uuid.UUID(claims["sub"])
        workspace_id = uuid.UUID(claims["wsp"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.info("access_token_jwt_malformed_claims", error=str(exc))
        raise AccessTokenError("invalid_token") from exc

    row = await session.get(OAuthAccessTokenRow, jti)
    if row is None:
        logger.info("access_token_jti_not_found", jti=str(jti))
        raise AccessTokenError("invalid_token")
    if row.revoked_at is not None:
        logger.info("access_token_revoked", jti=str(jti))
        raise AccessTokenError("invalid_token")
    if row.expires_at is not None and aware_utc(row.expires_at) <= datetime.now(UTC):
        logger.info("access_token_expired", jti=str(jti))
        raise AccessTokenError("invalid_token")

    scopes_raw = claims.get("scope") or ""

    # A malformed run claim is treated as ABSENT (no run scope), never as a
    # different run — the narrowing must never widen by way of a typo.
    run_raw = claims.get("run_id")
    try:
        run_id = uuid.UUID(str(run_raw)) if run_raw else None
    except (ValueError, AttributeError, TypeError):
        run_id = None

    return VerifiedAccessToken(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id=str(claims.get("client_id", "")),
        scopes=frozenset(s for s in str(scopes_raw).split() if s),
        jti=jti,
        run_id=run_id,
    )


__all__ = [
    "AccessTokenError",
    "VerifiedAccessToken",
    "verify_access_token_with_row",
]
