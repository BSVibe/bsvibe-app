"""Sentry install→grant connect — real-backend e2e (Lift 8, claim-later).

install-url (built from the operator's integration slug) + the public install
callback that exchanges ``code``+``installationId`` and parks an UNCLAIMED token
(no workspace binding — Sentry passes no state). Only Sentry's HTTP is mocked
(respx pass-through); store + provider run for real.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.api.webhooks import get_credential_cipher
from backend.connectors.auth import providers as providers_mod
from backend.connectors.auth import store
from backend.connectors.auth.service import set_app_credentials
from backend.router.accounts.crypto import CredentialCipher

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

TEST_KEY = b"0123456789abcdef0123456789abcdef"
_AUTHZ = "https://sentry.io/api/0/sentry-app-installations/inst-1/authorizations/"


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


async def _configure_sentry(sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher) -> None:
    async with sf() as s:
        await set_app_credentials(
            s,
            provider="sentry",
            client_id="cid",
            client_secret="sec",
            app_slug="bsvibe-int",
            cipher=cipher,
        )


async def test_install_url_reflects_configured_slug(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    not_yet = await client.get("/api/v1/connectors/oauth/sentry/install-url")
    assert not_yet.json()["configured"] is False

    await _configure_sentry(sf, cipher)
    r = await client.get("/api/v1/connectors/oauth/sentry/install-url")
    body = r.json()
    assert body["configured"] is True
    assert body["install_url"] == "https://sentry.io/sentry-apps/bsvibe-int/external-install/"


@respx.mock(assert_all_mocked=False)
async def test_install_callback_parks_unclaimed(
    respx_mock: respx.MockRouter,
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
) -> None:
    await _configure_sentry(sf, cipher)
    respx_mock.post(_AUTHZ).mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "sntrys_tok",
                "refreshToken": "sntrys_ref",
                "expiresAt": "2099-01-01T00:00:00Z",
            },
        )
    )

    cb = await client.get(
        "/api/v1/connectors/oauth/sentry/install/callback",
        params={"code": "grant", "installationId": "inst-1"},
    )
    assert cb.status_code in (302, 307), cb.text
    assert "sentry_install=pending" in cb.headers["location"]

    async with sf() as s:
        rows = await store.list_unclaimed(s, provider="sentry")
        assert len(rows) == 1
        assert rows[0].installation_ref == "inst-1"
        # token stored, claimable + decryptable
        claimed = await store.claim_unclaimed(s, unclaimed_id=rows[0].id, cipher=cipher)
    assert claimed is not None
    _, install_ref, token = claimed
    assert install_ref == "inst-1"
    assert token.access_token == "sntrys_tok"
    assert token.refresh_token == "sntrys_ref"


async def test_callback_when_not_configured_redirects_error(client: httpx.AsyncClient) -> None:
    cb = await client.get(
        "/api/v1/connectors/oauth/sentry/install/callback",
        params={"code": "grant", "installationId": "inst-x"},
    )
    assert cb.status_code in (302, 307)
    assert "connect_error=sentry" in cb.headers["location"]


@respx.mock(assert_all_mocked=False)
async def test_unclaimed_list_and_claim_via_rest(
    respx_mock: respx.MockRouter,
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
) -> None:
    await _configure_sentry(sf, cipher)
    respx_mock.post(_AUTHZ).mock(
        return_value=httpx.Response(201, json={"token": "t", "refreshToken": "r"})
    )
    await client.get(
        "/api/v1/connectors/oauth/sentry/install/callback",
        params={"code": "g", "installationId": "inst-1"},
    )
    listed = await client.get("/api/v1/connectors/oauth/unclaimed")
    assert listed.status_code == 200
    items = listed.json()["unclaimed"]
    assert len(items) == 1 and items[0]["installation_ref"] == "inst-1"

    claim = await client.post(f"/api/v1/connectors/oauth/unclaimed/{items[0]['id']}/claim")
    assert claim.status_code == 200, claim.text
    assert claim.json() == {"connector": "sentry", "claimed": True}

    # claimed → unclaimed list now empty
    again = await client.get("/api/v1/connectors/oauth/unclaimed")
    assert again.json()["unclaimed"] == []


async def test_claim_missing_404(client: httpx.AsyncClient) -> None:
    import uuid as _uuid

    r = await client.post(f"/api/v1/connectors/oauth/unclaimed/{_uuid.uuid4()}/claim")
    assert r.status_code == 404
