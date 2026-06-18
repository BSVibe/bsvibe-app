"""Persistence helpers for the connector OAuth dance (Lift 0).

Thin async repository over the two AuthStrategy tables + the existing
``connector_accounts`` binding. Keeps the endpoint layer free of raw SQL and
keeps the single-use / encrypt-on-write invariants in one place:

* :func:`create_pending` / :func:`claim_pending` — the CSRF state + PKCE
  verifier held between ``/start`` and ``/callback``. ``claim`` deletes the
  row as it reads it (single-use: replay → miss → 400 at the route).
* :func:`get_or_create_account` — resolve (or mint) the ``connector_accounts``
  binding the token attaches to. OAuth-first connect needs no pre-existing
  binding; we mint one with a webhook token + placeholder signing secret
  (the same placeholder pattern inbound connectors already use).
* :func:`upsert_token` — encrypt + persist the provider's token set, one row
  per binding (insert or update).
"""

from __future__ import annotations

import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.auth.db import (
    ConnectorOAuthPendingRow,
    ConnectorOAuthTokenRow,
    ConnectorOAuthUnclaimedRow,
)
from backend.connectors.auth.tokenset import TokenSet
from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher

# Inbound/OAuth-first bindings have no real webhook signing secret yet; the
# column is NOT NULL, so we store a stable non-secret placeholder (mirrors the
# PWA's INBOUND_SECRET_PLACEHOLDER). A real secret can be set later if the
# connector also receives webhooks.
_OAUTH_SECRET_PLACEHOLDER = "no-webhook-secret"  # noqa: S105 — placeholder, not a secret
_TOKEN_BYTES = 32


async def create_pending(
    session: AsyncSession,
    *,
    state: str,
    provider: str,
    workspace_id: uuid.UUID,
    code_verifier: str,
    redirect_uri: str,
) -> ConnectorOAuthPendingRow:
    """Stash the in-flight CSRF state + PKCE verifier for a connect attempt."""
    row = ConnectorOAuthPendingRow(
        state=state,
        provider=provider,
        workspace_id=workspace_id,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
    )
    session.add(row)
    await session.flush()
    return row


async def claim_pending(
    session: AsyncSession, *, state: str, provider: str
) -> ConnectorOAuthPendingRow | None:
    """Fetch + delete the pending row (single-use). ``None`` if absent/mismatch."""
    row = await session.get(ConnectorOAuthPendingRow, state)
    if row is None or row.provider != provider:
        return None
    # Detach the field values before deletion so the caller can still read them.
    claimed = ConnectorOAuthPendingRow(
        state=row.state,
        provider=row.provider,
        workspace_id=row.workspace_id,
        code_verifier=row.code_verifier,
        redirect_uri=row.redirect_uri,
        created_at=row.created_at,
    )
    await session.delete(row)
    await session.flush()
    return claimed


async def get_or_create_account(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    connector: str,
    cipher: CredentialCipher,
) -> ConnectorAccountRow:
    """Resolve the workspace's binding for ``connector``, minting one if absent."""
    existing = (
        await session.execute(
            select(ConnectorAccountRow).where(
                ConnectorAccountRow.workspace_id == workspace_id,
                ConnectorAccountRow.connector == connector,
                ConnectorAccountRow.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = ConnectorAccountRow(
        workspace_id=workspace_id,
        connector=connector,
        webhook_token=secrets.token_urlsafe(_TOKEN_BYTES),
        signing_secret_ciphertext=cipher.encrypt(_OAUTH_SECRET_PLACEHOLDER),
        external_ref=None,
        delivery_config={},
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return row


async def upsert_token(
    session: AsyncSession,
    *,
    connector_account_id: uuid.UUID,
    provider: str,
    token: TokenSet,
    cipher: CredentialCipher,
) -> ConnectorOAuthTokenRow:
    """Encrypt + persist ``token`` for a binding (insert or update in place)."""
    existing = (
        await session.execute(
            select(ConnectorOAuthTokenRow).where(
                ConnectorOAuthTokenRow.connector_account_id == connector_account_id
            )
        )
    ).scalar_one_or_none()

    access_ct = cipher.encrypt(token.access_token)
    refresh_ct = cipher.encrypt(token.refresh_token) if token.refresh_token else None
    scopes = list(token.scopes)

    if existing is not None:
        existing.provider = provider
        existing.access_token_ciphertext = access_ct
        existing.refresh_token_ciphertext = refresh_ct
        existing.expires_at = token.expires_at
        existing.scopes = scopes
        existing.account_label = token.account_label
        # Lift E48 — every path that calls upsert_token has just received a
        # fresh token from the provider (initial connect, refresh-on-resolve,
        # or a founder-triggered re-OAuth after needs_reauth). A row that
        # carries fresh credentials by definition no longer needs re-auth,
        # so always reset the status. Without this the PWA card stayed in
        # the "Reconnect" pill state even after a successful re-OAuth —
        # the founder kept clicking with no visible result until the next
        # dispatch ran resolve's refresh-success path.
        existing.status = "active"
        await session.flush()
        return existing

    row = ConnectorOAuthTokenRow(
        connector_account_id=connector_account_id,
        provider=provider,
        access_token_ciphertext=access_ct,
        refresh_token_ciphertext=refresh_ct,
        expires_at=token.expires_at,
        scopes=scopes,
        account_label=token.account_label,
    )
    session.add(row)
    await session.flush()
    return row


async def create_unclaimed(
    session: AsyncSession,
    *,
    provider: str,
    installation_ref: str,
    account_label: str | None,
    token: TokenSet,
    cipher: CredentialCipher,
) -> ConnectorOAuthUnclaimedRow:
    """Park an exchanged token awaiting a workspace claim (encrypt-on-write)."""
    row = ConnectorOAuthUnclaimedRow(
        provider=provider,
        installation_ref=installation_ref,
        account_label=account_label,
        access_token_ciphertext=cipher.encrypt(token.access_token),
        refresh_token_ciphertext=(
            cipher.encrypt(token.refresh_token) if token.refresh_token else None
        ),
        expires_at=token.expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def list_unclaimed(
    session: AsyncSession, *, provider: str | None = None
) -> list[ConnectorOAuthUnclaimedRow]:
    """Unclaimed installs (optionally filtered by provider), newest first."""
    stmt = select(ConnectorOAuthUnclaimedRow).order_by(ConnectorOAuthUnclaimedRow.created_at.desc())
    if provider is not None:
        stmt = stmt.where(ConnectorOAuthUnclaimedRow.provider == provider)
    return list((await session.execute(stmt)).scalars().all())


async def claim_unclaimed(
    session: AsyncSession, *, unclaimed_id: uuid.UUID, cipher: CredentialCipher
) -> tuple[str, str, TokenSet] | None:
    """Fetch + delete an unclaimed row (single-use). Returns
    ``(provider, installation_ref, decrypted TokenSet)`` or ``None`` if absent.
    """
    row = await session.get(ConnectorOAuthUnclaimedRow, unclaimed_id)
    if row is None:
        return None
    token = TokenSet(
        access_token=cipher.decrypt(row.access_token_ciphertext),
        refresh_token=(
            cipher.decrypt(row.refresh_token_ciphertext) if row.refresh_token_ciphertext else None
        ),
        expires_at=row.expires_at,
        account_label=row.account_label,
    )
    provider, installation_ref = row.provider, row.installation_ref
    await session.delete(row)
    await session.flush()
    return provider, installation_ref, token


__all__ = [
    "claim_pending",
    "claim_unclaimed",
    "create_pending",
    "create_unclaimed",
    "get_or_create_account",
    "list_unclaimed",
    "upsert_token",
]
