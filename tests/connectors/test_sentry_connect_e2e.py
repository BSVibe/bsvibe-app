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
from backend.connectors.auth.app_credentials import upsert_app_credentials
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
        await upsert_app_credentials(
            s,
            provider="sentry",
            app_id="",
            client_id="cid",
            client_secret="sec",
            private_key_pem="",
            app_slug="bsvibe-int",
            webhook_secret=None,
            html_url=None,
            cipher=cipher,
        )
        await s.commit()


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
        # token stored under the installation ref, claimable + decryptable
        claimed = await store.claim_by_installation(
            s, provider="sentry", installation_ref="inst-1", cipher=cipher
        )
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
async def test_claim_via_rest_with_installation_ref(
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
    # H2 — no GET /unclaimed. The founder presents the ref from their Sentry org.
    claim = await client.post(
        "/api/v1/connectors/oauth/unclaimed/claim",
        json={"provider": "sentry", "installation_ref": "inst-1"},
    )
    assert claim.status_code == 200, claim.text
    assert claim.json() == {"connector": "sentry", "claimed": True}

    # single-use: the same ref no longer claims.
    again = await client.post(
        "/api/v1/connectors/oauth/unclaimed/claim",
        json={"provider": "sentry", "installation_ref": "inst-1"},
    )
    assert again.status_code == 404


async def test_claim_wrong_ref_is_404(
    respx_mock: respx.MockRouter,
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
) -> None:
    """A ref you do not possess is indistinguishable from a nonexistent one."""
    await _configure_sentry(sf, cipher)
    respx_mock.post(_AUTHZ).mock(
        return_value=httpx.Response(201, json={"token": "t", "refreshToken": "r"})
    )
    await client.get(
        "/api/v1/connectors/oauth/sentry/install/callback",
        params={"code": "g", "installationId": "inst-1"},
    )
    # A pending install exists (inst-1), but the caller presents a ref they do
    # not possess — same 404 as a nonexistent one, no oracle.
    r = await client.post(
        "/api/v1/connectors/oauth/unclaimed/claim",
        json={"provider": "sentry", "installation_ref": "inst-guessed"},
    )
    assert r.status_code == 404


async def test_claim_missing_404(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/connectors/oauth/unclaimed/claim",
        json={"provider": "sentry", "installation_ref": "nope"},
    )
    assert r.status_code == 404
