"""Instance-global OAuth App credential store (Lift 1.5).

bsvibe acts as an OAuth *client* of a third party, which requires a registered
App (client_id/secret + — for GitHub Apps — app_id + a private key). Those App
credentials are minted once (via the GitHub App Manifest flow, or set in env)
and shared across the whole instance — per-workspace *tokens* hang off them.

This module is the encrypt-on-write / decrypt-on-read boundary over
:class:`backend.connectors.auth.db.ConnectorOAuthAppCredentialRow`. Plaintext
secrets live only in the :class:`AppCredentials` dataclass in memory; the row
holds ciphertext.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.auth.db import ConnectorOAuthAppCredentialRow
from backend.router.accounts.crypto import CredentialCipher


@dataclass(frozen=True)
class AppCredentials:
    """Decrypted OAuth App credentials for one provider."""

    provider: str
    app_id: str
    client_id: str
    client_secret: str
    private_key_pem: str
    app_slug: str | None = None
    webhook_secret: str | None = None
    html_url: str | None = None


async def upsert_app_credentials(
    session: AsyncSession,
    *,
    provider: str,
    app_id: str,
    client_id: str,
    client_secret: str,
    private_key_pem: str,
    app_slug: str | None,
    webhook_secret: str | None,
    html_url: str | None,
    cipher: CredentialCipher,
) -> ConnectorOAuthAppCredentialRow:
    """Encrypt + persist the App credentials for ``provider`` (one row, upsert)."""
    secret_ct = cipher.encrypt(client_secret)
    pem_ct = cipher.encrypt(private_key_pem)
    webhook_ct = cipher.encrypt(webhook_secret) if webhook_secret else None

    existing = await session.get(ConnectorOAuthAppCredentialRow, provider)
    if existing is not None:
        existing.app_id = app_id
        existing.app_slug = app_slug
        existing.client_id = client_id
        existing.client_secret_ciphertext = secret_ct
        existing.private_key_pem_ciphertext = pem_ct
        existing.webhook_secret_ciphertext = webhook_ct
        existing.html_url = html_url
        await session.flush()
        return existing

    row = ConnectorOAuthAppCredentialRow(
        provider=provider,
        app_id=app_id,
        app_slug=app_slug,
        client_id=client_id,
        client_secret_ciphertext=secret_ct,
        private_key_pem_ciphertext=pem_ct,
        webhook_secret_ciphertext=webhook_ct,
        html_url=html_url,
    )
    session.add(row)
    await session.flush()
    return row


async def get_app_credentials(
    session: AsyncSession, *, provider: str, cipher: CredentialCipher
) -> AppCredentials | None:
    """Return the decrypted App credentials for ``provider`` or ``None``."""
    row = await session.get(ConnectorOAuthAppCredentialRow, provider)
    if row is None:
        return None
    return AppCredentials(
        provider=row.provider,
        app_id=row.app_id,
        client_id=row.client_id,
        client_secret=cipher.decrypt(row.client_secret_ciphertext),
        private_key_pem=cipher.decrypt(row.private_key_pem_ciphertext),
        app_slug=row.app_slug,
        webhook_secret=(
            cipher.decrypt(row.webhook_secret_ciphertext) if row.webhook_secret_ciphertext else None
        ),
        html_url=row.html_url,
    )


__all__ = ["AppCredentials", "get_app_credentials", "upsert_app_credentials"]
