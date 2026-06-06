"""connector_oauth_app_credentials — instance-global OAuth App creds (Lift 1.5).

The GitHub App Manifest flow lets a founder create the bsvibe GitHub App from
inside the PWA (one click) instead of hand-creating it on GitHub and pasting
four secrets into env. The resulting App credentials (client_id/secret, app_id,
private key PEM, webhook secret) are instance-global (ONE app per provider —
the standard SaaS pattern: per-workspace *tokens*, one *app*), so they live in
their own one-row-per-provider table, encrypted at rest via the same
CredentialCipher as every other secret.
"""

from __future__ import annotations

import pytest

from backend.connectors.auth.app_credentials import (
    AppCredentials,
    get_app_credentials,
    upsert_app_credentials,
)
from backend.connectors.auth.db import ConnectorOAuthAppCredentialRow
from backend.router.accounts.crypto import CredentialCipher
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

TEST_KEY = b"0123456789abcdef0123456789abcdef"
_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
_SECRET = "ghsecret-xyz"  # noqa: S105 — test fixture


def _cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


async def test_upsert_then_get_roundtrips_decrypted() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        await upsert_app_credentials(
            s,
            provider="github",
            app_id="123456",
            app_slug="bsvibe-dev",
            client_id="Iv1.cid",
            client_secret=_SECRET,
            private_key_pem=_PEM,
            webhook_secret="whsec",
            html_url="https://github.com/apps/bsvibe-dev",
            cipher=cipher,
        )
        await s.commit()

        creds = await get_app_credentials(s, provider="github", cipher=cipher)

    assert isinstance(creds, AppCredentials)
    assert creds.app_id == "123456"
    assert creds.app_slug == "bsvibe-dev"
    assert creds.client_id == "Iv1.cid"
    assert creds.client_secret == _SECRET
    assert creds.private_key_pem == _PEM
    assert creds.webhook_secret == "whsec"


async def test_secrets_are_encrypted_at_rest() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        await upsert_app_credentials(
            s,
            provider="github",
            app_id="1",
            app_slug=None,
            client_id="Iv1.cid",
            client_secret=_SECRET,
            private_key_pem=_PEM,
            webhook_secret=None,
            html_url=None,
            cipher=cipher,
        )
        await s.commit()
        row = await s.get(ConnectorOAuthAppCredentialRow, "github")

    assert row is not None
    # Ciphertext is not the plaintext.
    assert row.client_secret_ciphertext != _SECRET
    assert row.private_key_pem_ciphertext != _PEM
    assert cipher.decrypt(row.client_secret_ciphertext) == _SECRET
    # Optional webhook secret left unset.
    assert row.webhook_secret_ciphertext is None


async def test_upsert_is_idempotent_single_row_per_provider() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        for secret in ("first", "second"):
            await upsert_app_credentials(
                s,
                provider="github",
                app_id="1",
                app_slug=None,
                client_id="Iv1.cid",
                client_secret=secret,
                private_key_pem=_PEM,
                webhook_secret=None,
                html_url=None,
                cipher=cipher,
            )
        await s.commit()

        creds = await get_app_credentials(s, provider="github", cipher=cipher)
        from sqlalchemy import func, select  # noqa: PLC0415

        count = (
            await s.execute(select(func.count()).select_from(ConnectorOAuthAppCredentialRow))
        ).scalar_one()

    assert creds is not None
    assert creds.client_secret == "second"
    assert count == 1


async def test_get_missing_provider_returns_none() -> None:
    async with memory_session() as s:
        creds = await get_app_credentials(s, provider="github", cipher=_cipher())
    assert creds is None
