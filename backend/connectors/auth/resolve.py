"""resolve_connector_credentials — binding → ``{"token": …}`` for API calls.

The single resolution point every connector API call routes through. OAuth
token takes precedence; absent one, the legacy signing secret is used, so
every connector that exists today keeps working unchanged until OAuth is
wired for it.

This is for the *API-call* credential (outbound delivery, bulk import), NOT
the inbound webhook signing secret used for HMAC signature verification — that
stays on ``ConnectorAccountRow.signing_secret_ciphertext`` and is read
directly by the webhook resolver.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.auth.db import ConnectorOAuthTokenRow
from backend.connectors.auth.providers import OAuthProvider, get_provider
from backend.connectors.auth.sentry import SentryProvider
from backend.connectors.auth.store import upsert_token
from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher

# Refresh a little before actual expiry so an in-flight call doesn't race the
# boundary.
_REFRESH_SKEW = timedelta(minutes=5)


def _expiring_soon(token_row: ConnectorOAuthTokenRow) -> bool:
    exp = token_row.expires_at
    if exp is None:
        return False
    # SQLite round-trips DateTime(timezone=True) as naive — assume UTC so the
    # comparison never raises (sqlite-naive-datetime-system-tz-silent-shift).
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return exp <= datetime.now(tz=UTC) + _REFRESH_SKEW


class ConnectorReauthRequired(Exception):
    """Lift E45 — the bound OAuth refresh token can no longer be exchanged
    for a new access token (provider returned ``bad_refresh_token`` / 401 /
    similar). The founder must run the OAuth flow again for this account.
    The exception carries the ``account_id`` + ``provider`` so the caller
    can surface a precise "reconnect <provider>" signal instead of the
    raw provider error string (which travels through the dispatch + push
    paths as an opaque ``Authentication failed`` stack).
    """

    def __init__(self, *, account_id: uuid.UUID, provider: str, cause: str) -> None:
        super().__init__(f"connector {provider} (account={account_id}) needs re-OAuth: {cause}")
        self.account_id = account_id
        self.provider = provider
        self.cause = cause


async def _read_token_row(
    session: AsyncSession, account_id: uuid.UUID, *, lock: bool = False
) -> ConnectorOAuthTokenRow | None:
    """Read the account's token row; ``lock`` takes ``FOR UPDATE`` and bypasses
    the identity map so the values are the committed, current ones."""
    stmt = select(ConnectorOAuthTokenRow).where(
        ConnectorOAuthTokenRow.connector_account_id == account_id
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _refresh_in_own_transaction(
    session: AsyncSession,
    *,
    account: ConnectorAccountRow,
    cipher: CredentialCipher,
    provider: OAuthProvider,
) -> str | None:
    """Refresh ``account``'s token under a row lock, in its OWN transaction.

    ``resolve_connector_credentials`` runs inside the caller's transaction, so
    the lock/commit cycle must never touch it: a separate session from the same
    engine takes ``SELECT ... FOR UPDATE`` on the token row, re-reads it, and
    commits only itself (releasing the lock). Returns the access token, or
    ``None`` when the token row vanished.
    """
    async with AsyncSession(bind=session.bind, expire_on_commit=False) as own:
        token_row = await _read_token_row(own, account.id, lock=True)
        if token_row is None:
            await own.rollback()
            return None
        if not token_row.refresh_token_ciphertext or not _expiring_soon(token_row):
            # Someone else refreshed while we waited (or nothing to refresh).
            access = cipher.decrypt(token_row.access_token_ciphertext)
            await own.rollback()
            return access
        refresh_token = cipher.decrypt(token_row.refresh_token_ciphertext)
        refreshed = None
        if isinstance(provider, SentryProvider):
            # Sentry refresh is installation-scoped; the installationId lives on
            # the binding's external_ref (set at claim time).
            if account.external_ref:
                refreshed = await provider.refresh_installation(
                    installation_id=account.external_ref, refresh_token=refresh_token
                )
        else:
            try:
                refreshed = await provider.refresh(refresh_token=refresh_token)
            except Exception as exc:  # noqa: BLE001 — provider-specific; classify as reauth-needed
                # Flag needs_reauth only if the refresh token (re-read under the
                # lock) is still the one that just failed; if it rotated
                # meanwhile, proceed with the new token.
                current = await _read_token_row(own, account.id, lock=True)
                if (
                    current is not None
                    and current.refresh_token_ciphertext
                    and cipher.decrypt(current.refresh_token_ciphertext) != refresh_token
                ):
                    access = cipher.decrypt(current.access_token_ciphertext)
                    await own.rollback()
                    return access
                if current is not None:
                    current.status = "needs_reauth"
                    await own.commit()
                else:
                    await own.rollback()
                raise ConnectorReauthRequired(
                    account_id=account.id,
                    provider=token_row.provider,
                    cause=str(exc),
                ) from exc
        if refreshed is not None:
            token_row = await upsert_token(
                own,
                connector_account_id=account.id,
                provider=token_row.provider,
                token=refreshed,
                cipher=cipher,
            )
            # A successful refresh clears any prior needs_reauth flag.
            if token_row.status != "active":
                token_row.status = "active"
        access = cipher.decrypt(token_row.access_token_ciphertext)
        await own.commit()  # persist the rotated token + release the row lock
        return access


async def resolve_connector_credentials(
    session: AsyncSession,
    *,
    account: ConnectorAccountRow,
    cipher: CredentialCipher,
) -> dict[str, str]:
    """Return the API credential for ``account`` (OAuth token, else secret).

    Never commits or rolls back ``session`` — it runs inside the caller's
    transaction. A needed refresh happens in a separate, locked transaction.
    """
    token_row = await _read_token_row(session, account.id)

    if token_row is not None:
        if _expiring_soon(token_row) and token_row.refresh_token_ciphertext:
            provider = get_provider(token_row.provider)
            if provider is not None and provider.refreshable:
                access = await _refresh_in_own_transaction(
                    session, account=account, cipher=cipher, provider=provider
                )
                if access is None:
                    return {"token": cipher.decrypt(account.signing_secret_ciphertext)}
                # Let the caller's identity map see the committed values.
                with session.no_autoflush:
                    await session.execute(
                        select(ConnectorOAuthTokenRow)
                        .where(ConnectorOAuthTokenRow.connector_account_id == account.id)
                        .execution_options(populate_existing=True)
                    )
                return {"token": access}
        return {"token": cipher.decrypt(token_row.access_token_ciphertext)}

    # Legacy path — no OAuth token bound yet.
    return {"token": cipher.decrypt(account.signing_secret_ciphertext)}


async def resolve_github_token(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    cipher: CredentialCipher,
) -> str | None:
    """The workspace's github git credential, or ``None`` when unconnected.

    Repo-AGNOSTIC on purpose. ``resolve_github_binding`` (delivery) additionally
    requires ``delivery_config['repo']`` because it pushes to that one target;
    a clone of an ARBITRARY repo (product bootstrap) must not inherit that
    constraint — the credential is account-scoped, not repo-scoped.

    Returns ``None`` rather than raising when no active github connector exists,
    so callers can fall back to an anonymous clone (public repos keep working).
    """
    account = (
        (
            await session.execute(
                select(ConnectorAccountRow).where(
                    ConnectorAccountRow.workspace_id == workspace_id,
                    ConnectorAccountRow.connector == "github",
                    ConnectorAccountRow.is_active.is_(True),
                )
            )
        )
        .scalars()
        .first()
    )
    if account is None:
        return None
    creds = await resolve_connector_credentials(session, account=account, cipher=cipher)
    return creds.get("token") or None


__all__ = [
    "ConnectorReauthRequired",
    "resolve_connector_credentials",
    "resolve_github_token",
]
