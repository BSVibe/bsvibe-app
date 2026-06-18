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
from backend.connectors.auth.providers import get_provider
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


async def resolve_connector_credentials(
    session: AsyncSession,
    *,
    account: ConnectorAccountRow,
    cipher: CredentialCipher,
) -> dict[str, str]:
    """Return the API credential for ``account`` (OAuth token, else secret)."""
    token_row = (
        await session.execute(
            select(ConnectorOAuthTokenRow).where(
                ConnectorOAuthTokenRow.connector_account_id == account.id
            )
        )
    ).scalar_one_or_none()

    if token_row is not None:
        if _expiring_soon(token_row) and token_row.refresh_token_ciphertext:
            provider = get_provider(token_row.provider)
            if provider is not None and provider.refreshable:
                refresh_token = cipher.decrypt(token_row.refresh_token_ciphertext)
                refreshed = None
                if isinstance(provider, SentryProvider):
                    # Sentry refresh is installation-scoped; the installationId
                    # lives on the binding's external_ref (set at claim time).
                    if account.external_ref:
                        refreshed = await provider.refresh_installation(
                            installation_id=account.external_ref, refresh_token=refresh_token
                        )
                else:
                    try:
                        refreshed = await provider.refresh(refresh_token=refresh_token)
                    except Exception as exc:  # noqa: BLE001 — provider-specific; classify as reauth-needed
                        # Lift E46 — persist the needs_reauth state so the
                        # connectors API + PWA card can surface a Reconnect
                        # CTA. The flush + commit are the caller's
                        # responsibility (mirrors the successful-refresh
                        # ``upsert_token`` path); raising aborts the
                        # transaction only when the caller has not yet
                        # committed.
                        token_row.status = "needs_reauth"
                        await session.flush()
                        await session.commit()
                        raise ConnectorReauthRequired(
                            account_id=account.id,
                            provider=token_row.provider,
                            cause=str(exc),
                        ) from exc
                if refreshed is not None:
                    token_row = await upsert_token(
                        session,
                        connector_account_id=account.id,
                        provider=token_row.provider,
                        token=refreshed,
                        cipher=cipher,
                    )
                    # Lift E46 — a successful refresh clears any prior
                    # needs_reauth flag so a re-OAuth → first dispatch
                    # returns the card to the active state in the PWA.
                    if token_row.status != "active":
                        token_row.status = "active"
                        await session.flush()
        return {"token": cipher.decrypt(token_row.access_token_ciphertext)}

    # Legacy path — no OAuth token bound yet.
    return {"token": cipher.decrypt(account.signing_secret_ciphertext)}


__all__ = ["ConnectorReauthRequired", "resolve_connector_credentials"]
