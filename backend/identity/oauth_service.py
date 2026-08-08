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
    OAuthDeviceCodeRow,
    OAuthRefreshTokenRow,
)
from backend.identity.oauth_db import (
    aware_utc as _aware,
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


# ---------------------------------------------------------------------------
# Personal access tokens
# ---------------------------------------------------------------------------

#: Labels a row as founder-created rather than grant-issued. The label is the
#: ONLY thing distinguishing a PAT — everything else (verification, revocation,
#: introspection, scopes) is the shared access-token machinery.
PAT_LABEL_PREFIX = "pat:"
#: ``client_id`` stamped on a PAT. There is no OAuth client behind one, but the
#: column is NOT NULL and the value shows up in audit logs, so name it honestly.
PAT_CLIENT_ID = "bsvibe-pat"


@dataclass(frozen=True)
class IssuedPat:
    """A freshly minted PAT. ``token`` is returned exactly once, never re-derivable."""

    token: str
    id: uuid.UUID
    name: str
    scope: list[str]
    issued_at: datetime
    expires_at: datetime | None


async def issue_pat(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    name: str,
    scope: list[str],
    issuer: str,
    expires_in_days: int | None = None,
    now: datetime | None = None,
) -> IssuedPat:
    """Mint a Personal Access Token for browserless clients.

    The OAuth loopback flow needs a browser on the same machine as the MCP
    client; over a remote tunnel, SSH, or a cron job there is none. A PAT is
    the static-bearer escape hatch, and it is deliberately the SAME artefact as
    every other access token so ``/revoke``, ``/introspect`` and the ``mcp:*``
    scopes keep working without a parallel code path.

    ``expires_in_days=None`` (the default) leaves ``expires_at`` NULL — never
    expires. No refresh token is minted: a credential able to re-mint its own
    access is a durable foothold, not a machine credential (the same reasoning
    :func:`issue_run_task_token` documents).

    The caller owns the transaction; this only flushes.
    """
    n = now or _utcnow()
    expires_at = n + timedelta(days=expires_in_days) if expires_in_days is not None else None
    access_id = uuid.uuid4()
    session.add(
        OAuthAccessTokenRow(
            id=access_id,
            workspace_id=workspace_id,
            user_id=user_id,
            client_id=PAT_CLIENT_ID,
            scope=list(scope),
            issued_at=n,
            expires_at=expires_at,
            label=f"{PAT_LABEL_PREFIX}{name}",
        )
    )
    await session.flush()
    token = issue_access_token(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id=PAT_CLIENT_ID,
        scope=list(scope),
        jti=access_id,
        issued_at=int(n.timestamp()),
        expires_at=int(expires_at.timestamp()) if expires_at is not None else None,
        issuer=issuer,
    )
    return IssuedPat(
        token=token,
        id=access_id,
        name=name,
        scope=list(scope),
        issued_at=n,
        expires_at=expires_at,
    )


async def list_pats(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
) -> list[OAuthAccessTokenRow]:
    """Live PATs for a workspace, newest first.

    Revoked rows are dropped and grant-issued tokens never appear — the
    founder's list is of credentials they created, not of every session.
    Expired-but-unrevoked rows still show so they can be seen and cleaned up.
    """
    stmt = (
        select(OAuthAccessTokenRow)
        .where(
            OAuthAccessTokenRow.workspace_id == workspace_id,
            OAuthAccessTokenRow.label.startswith(PAT_LABEL_PREFIX),
            OAuthAccessTokenRow.revoked_at.is_(None),
        )
        .order_by(OAuthAccessTokenRow.issued_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def revoke_pat(
    session: AsyncSession,
    *,
    pat_id: uuid.UUID,
    workspace_id: uuid.UUID,
    now: datetime | None = None,
) -> OAuthAccessTokenRow | None:
    """Revoke one PAT. ``None`` when it does not exist in ``workspace_id``.

    The workspace predicate is load-bearing: a bare id must not be enough to
    kill another tenant's credential.
    """
    n = now or _utcnow()
    row = await session.get(OAuthAccessTokenRow, pat_id)
    if (
        row is None
        or row.workspace_id != workspace_id
        or not (row.label or "").startswith(PAT_LABEL_PREFIX)
    ):
        return None
    if row.revoked_at is None:
        row.revoked_at = n
        await session.flush()
    return row


# ---------------------------------------------------------------------------
# Device authorization grant (RFC 8628)
# ---------------------------------------------------------------------------
# For the host that has no browser AND no way to paste a callback back — a
# remote tunnel, a chat-driven session, a headless box. The device polls; the
# human approves somewhere else entirely; the credential arrives without ever
# passing through a channel a person reads.

#: How long a pending device authorization lives. Long enough to walk to
#: another device and type a code; short enough that an abandoned one dies.
DEVICE_CODE_TTL = timedelta(minutes=10)
#: RFC 8628 §3.2 ``interval`` — the minimum seconds between polls.
DEVICE_POLL_INTERVAL_S = 5
#: Ambiguous glyphs (0/O, 1/I) are excluded: a human retypes this from a screen.
_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_USER_CODE_GROUP = 4


class DeviceExchangeOutcome(StrEnum):
    """Result of polling ``/token`` with a device code (RFC 8628 §3.5)."""

    APPROVED = "approved"
    AUTHORIZATION_PENDING = "authorization_pending"
    SLOW_DOWN = "slow_down"
    ACCESS_DENIED = "access_denied"
    EXPIRED_TOKEN = "expired_token"  # noqa: S105 — RFC 8628 §3.5 error code, not a secret
    INVALID_GRANT = "invalid_grant"


@dataclass(frozen=True)
class StartedDeviceAuthorization:
    """RFC 8628 §3.2 device-authorization response."""

    device_code: str
    user_code: str
    expires_in: int
    interval: int


def _gen_user_code() -> str:
    """``XXXX-XXXX`` from an unambiguous alphabet — typed by a human."""
    raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(_USER_CODE_GROUP * 2))
    return f"{raw[:_USER_CODE_GROUP]}-{raw[_USER_CODE_GROUP:]}"


def normalize_user_code(raw: str) -> str:
    """Fold what a human plausibly types back to the stored form.

    They will lowercase it, drop the dash, or paste it with a space. None of
    those should be a failed sign-in.
    """
    kept = [c for c in raw.upper() if c in _USER_CODE_ALPHABET]
    joined = "".join(kept)
    if len(joined) != _USER_CODE_GROUP * 2:
        return joined
    return f"{joined[:_USER_CODE_GROUP]}-{joined[_USER_CODE_GROUP:]}"


async def start_device_authorization(
    session: AsyncSession,
    *,
    client_id: str,
    scope: list[str],
    now: datetime | None = None,
) -> StartedDeviceAuthorization:
    """Open a pending device authorization and return both codes."""
    n = now or _utcnow()
    device_code = secrets.token_urlsafe(32)
    # Collisions are vanishingly unlikely but the column is unique, so retry
    # rather than surfacing an IntegrityError to a polling client.
    for _ in range(10):
        user_code = _gen_user_code()
        existing = await session.execute(
            select(OAuthDeviceCodeRow.id).where(OAuthDeviceCodeRow.user_code == user_code)
        )
        if existing.scalar_one_or_none() is None:
            break
    else:  # pragma: no cover — 32^8 space; ten collisions means something is wrong
        raise RuntimeError("could not allocate a unique user_code")

    session.add(
        OAuthDeviceCodeRow(
            client_id=client_id,
            device_code_hash=_sha256(device_code),
            user_code=user_code,
            scope=list(scope),
            issued_at=n,
            expires_at=n + DEVICE_CODE_TTL,
        )
    )
    await session.flush()
    return StartedDeviceAuthorization(
        device_code=device_code,
        user_code=user_code,
        expires_in=int(DEVICE_CODE_TTL.total_seconds()),
        interval=DEVICE_POLL_INTERVAL_S,
    )


async def lookup_device_code_by_user_code(
    session: AsyncSession, *, user_code: str
) -> OAuthDeviceCodeRow | None:
    """Find a pending request by the code a human typed."""
    stmt = select(OAuthDeviceCodeRow).where(
        OAuthDeviceCodeRow.user_code == normalize_user_code(user_code)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def approve_device_code(
    session: AsyncSession,
    *,
    user_code: str,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    now: datetime | None = None,
) -> OAuthDeviceCodeRow | None:
    """Bind the approving human to a pending request. ``None`` if unusable.

    The workspace is the APPROVER's. The device asserted nothing about identity
    and must not be able to.
    """
    n = now or _utcnow()
    row = await lookup_device_code_by_user_code(session, user_code=user_code)
    if row is None or _aware(row.expires_at) <= n:
        return None
    if row.approved_at is not None or row.denied_at is not None or row.consumed_at is not None:
        return None
    row.approved_at = n
    row.user_id = user_id
    row.workspace_id = workspace_id
    await session.flush()
    return row


async def deny_device_code(
    session: AsyncSession, *, user_code: str, now: datetime | None = None
) -> OAuthDeviceCodeRow | None:
    """Refuse a pending request so the device stops polling with a real answer."""
    n = now or _utcnow()
    row = await lookup_device_code_by_user_code(session, user_code=user_code)
    if row is None or _aware(row.expires_at) <= n:
        return None
    if row.approved_at is not None or row.denied_at is not None:
        return None
    row.denied_at = n
    await session.flush()
    return row


async def exchange_device_code(  # noqa: PLR0911 — RFC 8628 §3.5 answer table
    session: AsyncSession,
    *,
    device_code: str,
    client_id: str,
    issuer: str,
    now: datetime | None = None,
) -> tuple[DeviceExchangeOutcome, IssuedTokenPair | None]:
    """Poll a device code. Returns the RFC 8628 outcome and, once, the tokens."""
    n = now or _utcnow()
    stmt = select(OAuthDeviceCodeRow).where(
        OAuthDeviceCodeRow.device_code_hash == _sha256(device_code)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    # An unknown code and a wrong client are the same answer on the wire: never
    # confirm that a code exists to a client that does not own it.
    if row is None or row.client_id != client_id or row.consumed_at is not None:
        return DeviceExchangeOutcome.INVALID_GRANT, None
    if _aware(row.expires_at) <= n:
        return DeviceExchangeOutcome.EXPIRED_TOKEN, None

    # Rate limit BEFORE reading the decision, so an impatient device cannot
    # poll its way around the interval.
    last = row.last_polled_at
    too_soon = last is not None and (n - _aware(last)).total_seconds() < DEVICE_POLL_INTERVAL_S
    row.last_polled_at = n
    await session.flush()
    if too_soon:
        return DeviceExchangeOutcome.SLOW_DOWN, None

    if row.denied_at is not None:
        return DeviceExchangeOutcome.ACCESS_DENIED, None
    if row.approved_at is None or row.user_id is None or row.workspace_id is None:
        return DeviceExchangeOutcome.AUTHORIZATION_PENDING, None

    row.consumed_at = n
    await session.flush()
    pair = await issue_token_pair(
        session,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        client_id=client_id,
        scope=list(row.scope),
        issuer=issuer,
        label=f"device:{client_id}",
        now=n,
    )
    return DeviceExchangeOutcome.APPROVED, pair


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
            # A NULL expiry means never expires (a PAT) — active, and RFC 7662
            # ``exp`` is simply omitted rather than fabricated.
            if (
                access_row is not None
                and access_row.revoked_at is None
                and (access_row.expires_at is None or _aware(access_row.expires_at) > n)
            ):
                return IntrospectionResult(
                    active=True,
                    sub=str(access_row.user_id),
                    workspace_id=str(access_row.workspace_id),
                    client_id=access_row.client_id,
                    scope=" ".join(access_row.scope),
                    exp=(
                        int(_aware(access_row.expires_at).timestamp())
                        if access_row.expires_at is not None
                        else None
                    ),
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
    "DEVICE_CODE_TTL",
    "DEVICE_POLL_INTERVAL_S",
    "PAT_CLIENT_ID",
    "PAT_LABEL_PREFIX",
    "REFRESH_TOKEN_TTL",
    "ClaimedCode",
    "CodeClaimOutcome",
    "DeviceExchangeOutcome",
    "IntrospectionResult",
    "IssuedPat",
    "IssuedTokenPair",
    "StartedDeviceAuthorization",
    "RefreshRotateOutcome",
    "RevokeKind",
    "approve_device_code",
    "claim_authorization_code",
    "deny_device_code",
    "exchange_device_code",
    "introspect_token",
    "issue_authorization_code",
    "issue_pat",
    "issue_token_pair",
    "list_pats",
    "lookup_device_code_by_user_code",
    "normalize_user_code",
    "revoke_pat",
    "revoke_token",
    "rotate_refresh_token",
    "start_device_authorization",
]
