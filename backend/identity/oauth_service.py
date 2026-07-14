"""OAuth 2.0 service layer — code issuance/claim, token mint/rotate/revoke.

Calls into :mod:`backend.identity.oauth_db` rows directly (not through a
Protocol — D1 keeps the surface minimal; the repository extraction
follows the wider Identity Repo pass). Transaction boundary belongs to
the caller (the route): this module never commits.

Public surface:

* :func:`issue_authorization_code` — persist a new ``oauth_codes`` row.
* :func:`claim_authorization_code` — atomic single-use claim + PKCE.
* :func:`issue_token_pair` — mint access + refresh token rows + return
  the wire-format JWT + raw refresh string.
* :func:`rotate_refresh_token` — single-use refresh rotation.
* :func:`revoke_token` — mark an access or refresh token revoked.
* :func:`introspect_token` — RFC 7662 introspection from raw JWT or
  refresh string.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.oauth_db import (
    OAuthAccessTokenRow,
    OAuthCodeRow,
    OAuthRefreshTokenRow,
)
from backend.identity.oauth_jwt import (
    ACCESS_TOKEN_AUDIENCE,
    issue_access_token,
    verify_access_token,
)
from backend.identity.oauth_keys import jwks_payload
from backend.identity.oauth_pkce import (
    redirect_uris_equivalent,
    verify_pkce,
)

# RFC 6749 §4.1.2 — short-lived authorization codes (5 min in our impl).
CODE_TTL = timedelta(minutes=5)
# Access token TTL — short enough that revocation latency is bounded.
ACCESS_TOKEN_TTL = timedelta(hours=1)
# Refresh token TTL — long enough that real users seldom re-auth.
REFRESH_TOKEN_TTL = timedelta(days=30)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    """SQLite drops tz-info on a round-trip; restore UTC for comparisons."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _gen_code() -> str:
    """43-char URL-safe random — matches RFC 6749 §10.10 entropy advice."""
    return secrets.token_urlsafe(32)


def _gen_refresh() -> str:
    """Opaque refresh token — 43 URL-safe chars."""
    return secrets.token_urlsafe(32)


def _sha256(data: str) -> bytes:
    return hashlib.sha256(data.encode("ascii")).digest()


# ---------------------------------------------------------------------------
# Authorization code
# ---------------------------------------------------------------------------


async def issue_authorization_code(
    session: AsyncSession,
    *,
    client_id: str,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    scope: list[str],
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
    now: datetime | None = None,
) -> str:
    """Persist a new authorization code, return the raw code."""
    n = now or _utcnow()
    code = _gen_code()
    row = OAuthCodeRow(
        code=code,
        client_id=client_id,
        user_id=user_id,
        workspace_id=workspace_id,
        scope=list(scope),
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        issued_at=n,
        expires_at=n + CODE_TTL,
    )
    session.add(row)
    await session.flush()
    return code


class CodeClaimOutcome(StrEnum):
    """Why an authorization-code claim failed (or succeeded)."""

    CLAIMED = "claimed"
    NOT_FOUND = "not_found"
    USED = "used"
    EXPIRED = "expired"
    CLIENT_MISMATCH = "client_mismatch"
    REDIRECT_URI_MISMATCH = "redirect_uri_mismatch"
    PKCE_MISMATCH = "pkce_mismatch"


@dataclass(frozen=True)
class ClaimedCode:
    """The validated principal + scope a successful claim yields."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    client_id: str
    scope: list[str]


async def claim_authorization_code(  # noqa: PLR0911 — OAuth state machine
    session: AsyncSession,
    *,
    code: str,
    expected_client_id: str,
    redirect_uri: str,
    code_verifier: str,
    now: datetime | None = None,
) -> tuple[CodeClaimOutcome, ClaimedCode | None]:
    """Atomically single-use-claim an authorization code.

    All cross-checks happen AFTER the row is flipped to ``used_at`` so a
    replay always reads ``used``, never ``claimed``.
    """
    n = now or _utcnow()
    row = await session.get(OAuthCodeRow, code, with_for_update=True)
    if row is None:
        return CodeClaimOutcome.NOT_FOUND, None
    if row.used_at is not None:
        return CodeClaimOutcome.USED, None
    if _aware(row.expires_at) <= n:
        return CodeClaimOutcome.EXPIRED, None
    # Flip first — exhausts the code regardless of which cross-check fails.
    row.used_at = n
    await session.flush()
    if row.client_id != expected_client_id:
        return CodeClaimOutcome.CLIENT_MISMATCH, None
    if not redirect_uris_equivalent(row.redirect_uri, redirect_uri):
        return CodeClaimOutcome.REDIRECT_URI_MISMATCH, None
    if not verify_pkce(row.code_challenge, row.code_challenge_method, code_verifier):
        return CodeClaimOutcome.PKCE_MISMATCH, None
    return CodeClaimOutcome.CLAIMED, ClaimedCode(
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        client_id=row.client_id,
        scope=list(row.scope),
    )


# ---------------------------------------------------------------------------
# Access + refresh token issuance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedTokenPair:
    """Result of a token mint — wire-format access + refresh strings."""

    access_token: str
    refresh_token: str
    access_token_id: uuid.UUID
    expires_in: int
    scope: list[str]


#: How long a dispatched executor task's token lives. Long enough for a coding task on a big
#: repo; short enough that a leaked one dies on its own. It sits on the founder's machine, in
#: a CLI subprocess — the blast radius is bounded by (one run) x (this window).
RUN_TASK_TOKEN_TTL = timedelta(minutes=90)


async def issue_run_task_token(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    issuer: str,
    now: datetime | None = None,
) -> str:
    """Mint the credential a dispatched executor task carries (T2).

    The executor is the user's LLM CLIENT: it acts on the run by calling BSVibe's tools over
    MCP, which needs a token. This is the narrowest one that works:

    * bound to ONE run (the ``run_id`` claim → :attr:`McpPrincipal.run_id`), so the work tools
      reach that run's worktree and nothing else;
    * short-lived (:data:`RUN_TASK_TOKEN_TTL`);
    * **no refresh token** — :func:`issue_token_pair` mints one, and a task that could re-mint
      its own access would be a durable foothold rather than a task credential;
    * an ordinary ``OAuthAccessTokenRow``, so revocation and expiry work exactly as they do
      for every other token.
    """
    n = now or _utcnow()
    access_id = uuid.uuid4()
    scope = ["mcp:read", "mcp:write"]
    session.add(
        OAuthAccessTokenRow(
            id=access_id,
            workspace_id=workspace_id,
            user_id=user_id,
            client_id="bsvibe-worker",
            scope=scope,
            issued_at=n,
            expires_at=n + RUN_TASK_TOKEN_TTL,
            label=f"executor task (run {run_id})",
        )
    )
    await session.flush()
    return issue_access_token(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="bsvibe-worker",
        scope=scope,
        jti=access_id,
        issued_at=int(n.timestamp()),
        expires_at=int((n + RUN_TASK_TOKEN_TTL).timestamp()),
        issuer=issuer,
        run_id=run_id,
    )


async def issue_token_pair(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    client_id: str,
    scope: list[str],
    issuer: str,
    label: str | None = None,
    now: datetime | None = None,
) -> IssuedTokenPair:
    """Mint a fresh access + refresh token pair."""
    n = now or _utcnow()
    access_id = uuid.uuid4()
    access_row = OAuthAccessTokenRow(
        id=access_id,
        workspace_id=workspace_id,
        user_id=user_id,
        client_id=client_id,
        scope=list(scope),
        issued_at=n,
        expires_at=n + ACCESS_TOKEN_TTL,
        label=label,
    )
    session.add(access_row)

    raw_refresh = _gen_refresh()
    refresh_row = OAuthRefreshTokenRow(
        access_token_id=access_id,
        token_hash=_sha256(raw_refresh),
        issued_at=n,
        expires_at=n + REFRESH_TOKEN_TTL,
    )
    session.add(refresh_row)
    await session.flush()

    access_token = issue_access_token(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id=client_id,
        scope=list(scope),
        jti=access_id,
        issued_at=int(n.timestamp()),
        expires_at=int((n + ACCESS_TOKEN_TTL).timestamp()),
        issuer=issuer,
    )
    return IssuedTokenPair(
        access_token=access_token,
        refresh_token=raw_refresh,
        access_token_id=access_id,
        expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
        scope=list(scope),
    )


class RefreshRotateOutcome(StrEnum):
    """Outcome of a refresh-token grant attempt."""

    ROTATED = "rotated"
    INVALID = "invalid"
    EXPIRED = "expired"
    REUSED = "reused"
    PARENT_REVOKED = "parent_revoked"


async def rotate_refresh_token(
    session: AsyncSession,
    *,
    refresh_token: str,
    client_id: str,
    issuer: str,
    now: datetime | None = None,
) -> tuple[RefreshRotateOutcome, IssuedTokenPair | None]:
    """Single-use rotation. Returns a fresh pair on success.

    On reuse detection (``used_at`` already populated) we DO NOT revoke
    the parent access token in D1 — the simpler "expired" rule applies.
    Reuse-detection-based revocation is reserved for a later lift.
    """
    n = now or _utcnow()
    hash_ = _sha256(refresh_token)
    stmt = select(OAuthRefreshTokenRow).where(OAuthRefreshTokenRow.token_hash == hash_)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return RefreshRotateOutcome.INVALID, None
    if _aware(row.expires_at) <= n:
        return RefreshRotateOutcome.EXPIRED, None
    if row.used_at is not None:
        return RefreshRotateOutcome.REUSED, None
    # Flip first.
    row.used_at = n
    await session.flush()

    parent = await session.get(OAuthAccessTokenRow, row.access_token_id)
    if parent is None or parent.revoked_at is not None:
        return RefreshRotateOutcome.PARENT_REVOKED, None
    if parent.client_id != client_id:
        return RefreshRotateOutcome.INVALID, None
    # Mint new pair carrying the parent's scope + workspace.
    pair = await issue_token_pair(
        session,
        user_id=parent.user_id,
        workspace_id=parent.workspace_id,
        client_id=parent.client_id,
        scope=list(parent.scope),
        issuer=issuer,
        label=parent.label,
        now=n,
    )
    return RefreshRotateOutcome.ROTATED, pair


# ---------------------------------------------------------------------------
# Revocation + introspection
# ---------------------------------------------------------------------------


class RevokeKind(StrEnum):
    ACCESS_TOKEN = "access_token"  # noqa: S105 — RFC 7009 token_type_hint label
    REFRESH_TOKEN = "refresh_token"  # noqa: S105 — RFC 7009 token_type_hint label


async def revoke_token(
    session: AsyncSession,
    *,
    token: str,
    hint: RevokeKind | None = None,
    issuer: str,
    now: datetime | None = None,
) -> bool:
    """RFC 7009 — best-effort revoke. Returns True if a row was touched.

    Tries access-token JWT first (or refresh-token if ``hint`` says so),
    then falls back to the other kind. Per RFC 7009 §2.2 the server
    SHOULD respond 200 either way; the boolean here is purely for tests.
    """
    n = now or _utcnow()
    if hint != RevokeKind.REFRESH_TOKEN:
        # Try as access-token JWT.
        try:
            claims = verify_access_token(token, issuer=issuer, jwks=jwks_payload())
            jti = uuid.UUID(claims["jti"])
            update_stmt = (
                update(OAuthAccessTokenRow)
                .where(
                    OAuthAccessTokenRow.id == jti,
                    OAuthAccessTokenRow.revoked_at.is_(None),
                )
                .values(revoked_at=n)
            )
            update_result = await session.execute(update_stmt)
            if (getattr(update_result, "rowcount", 0) or 0) > 0:
                return True
        except Exception:  # noqa: BLE001, S110 — RFC 7009 §2.2 mandates this
            pass
    # Refresh-token path.
    hash_ = _sha256(token)
    select_stmt = select(OAuthRefreshTokenRow).where(OAuthRefreshTokenRow.token_hash == hash_)
    row = (await session.execute(select_stmt)).scalar_one_or_none()
    if row is None:
        return False
    parent = await session.get(OAuthAccessTokenRow, row.access_token_id)
    if parent is not None and parent.revoked_at is None:
        parent.revoked_at = n
    if row.used_at is None:
        row.used_at = n
    await session.flush()
    return True


@dataclass(frozen=True)
class IntrospectionResult:
    """RFC 7662 fields surfaced from :func:`introspect_token`."""

    active: bool
    sub: str | None = None
    workspace_id: str | None = None
    client_id: str | None = None
    scope: str | None = None
    exp: int | None = None
    iat: int | None = None
    token_type: str | None = None
    aud: str | None = None
    iss: str | None = None
    jti: str | None = None


async def introspect_token(
    session: AsyncSession,
    *,
    token: str,
    issuer: str,
    hint: RevokeKind | None = None,
    now: datetime | None = None,
) -> IntrospectionResult:
    """RFC 7662 introspection. ``active=False`` on any failure."""
    n = now or _utcnow()
    # Try access-token JWT first.
    if hint != RevokeKind.REFRESH_TOKEN:
        try:
            claims = verify_access_token(token, issuer=issuer, jwks=jwks_payload())
            jti = uuid.UUID(claims["jti"])
            access_row = await session.get(OAuthAccessTokenRow, jti)
            if (
                access_row is not None
                and access_row.revoked_at is None
                and _aware(access_row.expires_at) > n
            ):
                return IntrospectionResult(
                    active=True,
                    sub=str(access_row.user_id),
                    workspace_id=str(access_row.workspace_id),
                    client_id=access_row.client_id,
                    scope=" ".join(access_row.scope),
                    exp=int(_aware(access_row.expires_at).timestamp()),
                    iat=int(_aware(access_row.issued_at).timestamp()),
                    token_type="access_token",  # noqa: S106 — RFC 7662 label
                    aud=ACCESS_TOKEN_AUDIENCE,
                    iss=issuer,
                    jti=str(jti),
                )
        except Exception:  # noqa: BLE001, S110 — RFC 7662 §2.2 inactive-on-error
            pass
    # Refresh-token path.
    hash_ = _sha256(token)
    stmt = select(OAuthRefreshTokenRow).where(OAuthRefreshTokenRow.token_hash == hash_)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return IntrospectionResult(active=False)
    if row.used_at is not None or _aware(row.expires_at) <= n:
        return IntrospectionResult(active=False)
    parent = await session.get(OAuthAccessTokenRow, row.access_token_id)
    if parent is None or parent.revoked_at is not None:
        return IntrospectionResult(active=False)
    return IntrospectionResult(
        active=True,
        sub=str(parent.user_id),
        workspace_id=str(parent.workspace_id),
        client_id=parent.client_id,
        scope=" ".join(parent.scope),
        exp=int(_aware(row.expires_at).timestamp()),
        iat=int(_aware(row.issued_at).timestamp()),
        token_type="refresh_token",  # noqa: S106 — RFC 7662 label
        iss=issuer,
    )


__all__ = [
    "ACCESS_TOKEN_TTL",
    "CODE_TTL",
    "REFRESH_TOKEN_TTL",
    "ClaimedCode",
    "CodeClaimOutcome",
    "IntrospectionResult",
    "IssuedTokenPair",
    "RefreshRotateOutcome",
    "RevokeKind",
    "claim_authorization_code",
    "introspect_token",
    "issue_authorization_code",
    "issue_token_pair",
    "revoke_token",
    "rotate_refresh_token",
]
