"""Operator OAuth-app credential setup for vanilla providers (slack/notion/discord).

github sets up its App via the manifest flow; slack / notion / discord have no
programmatic app-creation API, so the operator pastes the client_id/secret once
and we store them in ``connector_oauth_app_credentials`` (encrypted, instance-
global — the SaaS single-app model) and register the provider so every
workspace can then 1-click "Connect with X".
"""

from __future__ import annotations

import pytest

from backend.connectors.auth import providers as providers_mod
from backend.connectors.auth.app_credentials import get_app_credentials
from backend.connectors.auth.bootstrap import load_app_credential_providers
from backend.connectors.auth.providers import get_provider
from backend.connectors.auth.service import set_app_credentials
from backend.router.accounts.crypto import CredentialCipher
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

TEST_KEY = b"0123456789abcdef0123456789abcdef"


def _cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    snapshot = dict(providers_mod._REGISTRY)
    try:
        yield
    finally:
        providers_mod._REGISTRY.clear()
        providers_mod._REGISTRY.update(snapshot)


async def test_set_slack_creds_stores_encrypted_and_registers() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        await set_app_credentials(
            s, provider="slack", client_id="Iv1.cid", client_secret="sec", cipher=cipher
        )
        creds = await get_app_credentials(s, provider="slack", cipher=cipher)
    assert creds is not None
    assert creds.client_id == "Iv1.cid"
    assert creds.client_secret == "sec"
    prov = get_provider("slack")
    assert prov is not None
    assert prov.name == "slack"


async def test_set_creds_rejects_github() -> None:
    # github uses the manifest flow, not paste-creds.
    cipher = _cipher()
    async with memory_session() as s:
        with pytest.raises(ValueError, match="github"):
            await set_app_credentials(
                s, provider="github", client_id="x", client_secret="y", cipher=cipher
            )


async def test_set_creds_rejects_unknown_provider() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        with pytest.raises(ValueError):
            await set_app_credentials(
                s, provider="pager-duty", client_id="x", client_secret="y", cipher=cipher
            )


async def test_load_registers_vanilla_providers_from_db() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        await set_app_credentials(
            s, provider="notion", client_id="n", client_secret="ns", cipher=cipher
        )
        await set_app_credentials(
            s, provider="discord", client_id="d", client_secret="ds", cipher=cipher
        )
        # Simulate a restart: clear the live registry, then reload from DB.
        providers_mod._REGISTRY.clear()
        registered = await load_app_credential_providers(s, cipher)
    assert "notion" in registered
    assert "discord" in registered
    assert get_provider("notion") is not None
    assert get_provider("discord") is not None
