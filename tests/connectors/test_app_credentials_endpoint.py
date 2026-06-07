"""POST /api/v1/connectors/oauth/{provider}/app-credentials — operator setup.

Real-backend e2e: a founder pastes a vanilla provider's OAuth App client_id /
client_secret; the endpoint stores them encrypted + registers the provider so
workspaces can connect. github is rejected (manifest flow). Auth + cipher
overridden; store + registration run for real.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.api.webhooks import get_credential_cipher
from backend.connectors.auth import providers as providers_mod
from backend.connectors.auth.app_credentials import get_app_credentials
from backend.connectors.auth.providers import get_provider
from backend.router.accounts.crypto import CredentialCipher

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

TEST_KEY = b"0123456789abcdef0123456789abcdef"


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    snapshot = dict(providers_mod._REGISTRY)
    try:
        yield
    finally:
        providers_mod._REGISTRY.clear()
        providers_mod._REGISTRY.update(snapshot)


@pytest_asyncio.fixture
async def client(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: uuid.uuid4()
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_credential_cipher] = lambda: cipher
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_set_slack_app_credentials_stores_and_registers(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    r = await client.post(
        "/api/v1/connectors/oauth/slack/app-credentials",
        json={"client_id": "Iv1.cid", "client_secret": "sec"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True

    async with sf() as s:
        creds = await get_app_credentials(s, provider="slack", cipher=cipher)
    assert creds is not None
    assert creds.client_id == "Iv1.cid"
    assert creds.client_secret == "sec"
    assert get_provider("slack") is not None


async def test_set_github_app_credentials_rejected(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/connectors/oauth/github/app-credentials",
        json={"client_id": "x", "client_secret": "y"},
    )
    assert r.status_code == 400
    assert "github" in r.text.lower()


async def test_never_returns_the_secret(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/connectors/oauth/notion/app-credentials",
        json={"client_id": "Iv1.cid", "client_secret": "supersecret"},
    )
    assert r.status_code == 200
    assert "supersecret" not in r.text


async def test_set_sentry_app_credentials_with_slug(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    r = await client.post(
        "/api/v1/connectors/oauth/sentry/app-credentials",
        json={"client_id": "cid", "client_secret": "sec", "app_slug": "bsvibe-int"},
    )
    assert r.status_code == 200, r.text
    async with sf() as s:
        creds = await get_app_credentials(s, provider="sentry", cipher=cipher)
    assert creds is not None and creds.app_slug == "bsvibe-int"
    assert get_provider("sentry") is not None


async def test_set_sentry_without_slug_400(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/connectors/oauth/sentry/app-credentials",
        json={"client_id": "cid", "client_secret": "sec"},
    )
    assert r.status_code == 400
    assert "slug" in r.text.lower()
