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

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.auth.db import ConnectorOAuthTokenRow
from backend.connectors.auth.providers import get_provider
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
                refreshed = await provider.refresh(
                    refresh_token=cipher.decrypt(token_row.refresh_token_ciphertext)
                )
                token_row = await upsert_token(
                    session,
                    connector_account_id=account.id,
                    provider=token_row.provider,
                    token=refreshed,
                    cipher=cipher,
                )
        return {"token": cipher.decrypt(token_row.access_token_ciphertext)}

    # Legacy path — no OAuth token bound yet.
    return {"token": cipher.decrypt(account.signing_secret_ciphertext)}


__all__ = ["resolve_connector_credentials"]
