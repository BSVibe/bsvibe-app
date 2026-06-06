"""load_app_credential_providers — register providers from DB creds (Lift 1.5).

Once the founder sets up the GitHub App via the manifest flow, its credentials
live in the DB (not env). At startup (and right after the manifest callback)
those DB creds must be loaded into the live provider registry, taking
precedence over any env-configured provider — the manifest-minted App is the
one the founder just chose.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from backend.connectors.auth import providers as providers_mod
from backend.connectors.auth.app_credentials import upsert_app_credentials
from backend.connectors.auth.bootstrap import load_app_credential_providers
from backend.connectors.auth.github import GitHubAppProvider
from backend.connectors.auth.providers import get_provider, register_provider
from backend.router.accounts.crypto import CredentialCipher
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

TEST_KEY = b"0123456789abcdef0123456789abcdef"
_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"


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


async def _seed_github_app(session, cipher: CredentialCipher, *, client_id: str) -> None:
    await upsert_app_credentials(
        session,
        provider="github",
        app_id="123456",
        app_slug="bsvibe",
        client_id=client_id,
        client_secret="csecret",  # noqa: S106
        private_key_pem=_PEM,
        webhook_secret=None,
        html_url=None,
        cipher=cipher,
    )
    await session.commit()


async def test_loads_github_provider_from_db_with_service_token() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        await _seed_github_app(s, cipher, client_id="Iv1.fromdb")
        registered = await load_app_credential_providers(s, cipher)

    assert registered == ["github"]
    prov = get_provider("github")
    assert isinstance(prov, GitHubAppProvider)
    # app_id + private key present → installation-token capability on.
    assert prov.supports_service_token is True


async def test_db_creds_take_precedence_over_env_provider() -> None:
    cipher = _cipher()
    # Simulate an env-registered provider already in the registry.
    register_provider(
        GitHubAppProvider(client_id="Iv1.fromenv", client_secret="x")  # noqa: S106
    )
    async with memory_session() as s:
        await _seed_github_app(s, cipher, client_id="Iv1.fromdb")
        await load_app_credential_providers(s, cipher)

    prov = get_provider("github")
    assert prov is not None
    url = prov.authorize_url(state="s", code_challenge="c", redirect_uri="https://cb")
    client_id = parse_qs(urlsplit(url).query)["client_id"][0]
    assert client_id == "Iv1.fromdb"


async def test_no_db_creds_registers_nothing() -> None:
    async with memory_session() as s:
        registered = await load_app_credential_providers(s, _cipher())
    assert registered == []
