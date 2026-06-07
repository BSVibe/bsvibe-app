"""resolve_connector_credentials refreshes a Sentry token via its installation.

Sentry's refresh is installation-scoped (`refresh_installation(installationId)`),
not the generic `refresh(refresh_token)`. The installationId is stored on the
connector_account's external_ref at claim time; resolve must use it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from backend.connectors.auth import providers as providers_mod
from backend.connectors.auth import store
from backend.connectors.auth.providers import register_provider
from backend.connectors.auth.resolve import resolve_connector_credentials
from backend.connectors.auth.sentry import SentryProvider
from backend.connectors.auth.tokenset import TokenSet
from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

KEY = b"0123456789abcdef0123456789abcdef"
_AUTHZ = "https://sentry.io/api/0/sentry-app-installations/inst-1/authorizations/"


def _cipher() -> CredentialCipher:
    return CredentialCipher(KEY)


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    snapshot = dict(providers_mod._REGISTRY)
    try:
        yield
    finally:
        providers_mod._REGISTRY.clear()
        providers_mod._REGISTRY.update(snapshot)


@respx.mock
async def test_resolve_refreshes_sentry_via_installation() -> None:
    register_provider(SentryProvider(client_id="cid", client_secret="sec"))  # noqa: S106
    respx.post(_AUTHZ).mock(
        return_value=httpx.Response(201, json={"token": "fresh", "refreshToken": "newref"})
    )
    cipher = _cipher()
    async with memory_session() as s:
        account = ConnectorAccountRow(
            workspace_id=uuid.uuid4(),
            connector="sentry",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("x"),
            external_ref="inst-1",  # installationId stored at claim time
            delivery_config={},
            is_active=True,
        )
        s.add(account)
        await s.flush()
        await store.upsert_token(
            s,
            connector_account_id=account.id,
            provider="sentry",
            token=TokenSet(
                access_token="stale",
                refresh_token="oldref",
                expires_at=datetime.now(tz=UTC) - timedelta(minutes=1),  # expired → refresh
            ),
            cipher=cipher,
        )
        await s.commit()

        creds = await resolve_connector_credentials(s, account=account, cipher=cipher)
    assert creds["token"] == "fresh"  # refreshed via refresh_installation(inst-1)
    body = respx.calls.last.request
    import json  # noqa: PLC0415

    assert json.loads(body.content)["grant_type"] == "refresh_token"
