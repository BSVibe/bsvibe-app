"""GitHub App Manifest flow — real-backend e2e (Lift 1.5).

The founder-facing "Set up GitHub App" path so the App is created from inside
bsvibe (one click) instead of hand-made on GitHub + four secrets pasted into
env:

* ``POST /api/v1/connectors/oauth/github/app-manifest/start`` (founder) — return
  the GitHub ``settings/apps/new`` POST target (state in the query) + the
  manifest JSON the PWA auto-submits. redirect_url / callback_urls are built
  from the CONFIGURED base, never the request host.
* ``GET /api/v1/connectors/oauth/github/app-manifest/callback`` (public) —
  GitHub redirects here with ``?code=`` after the App is created; we exchange
  the code at ``/app-manifests/{code}/conversions``, store the minted App creds
  encrypted, register the GitHubAppProvider, and redirect to the PWA.

Only auth + cipher overridden; store + provider registration run for real.
GitHub's conversions HTTP is the only thing mocked (respx pass-through).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.api.v1.connector_oauth import _MANIFEST_PENDING_PROVIDER
from backend.api.webhooks import get_credential_cipher
from backend.config import get_settings
from backend.connectors.auth import providers as providers_mod
from backend.connectors.auth.app_credentials import get_app_credentials
from backend.connectors.auth.db import ConnectorOAuthPendingRow
from backend.connectors.auth.github import GitHubAppProvider
from backend.connectors.auth.providers import get_provider
from backend.router.accounts.crypto import CredentialCipher

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

TEST_KEY = b"0123456789abcdef0123456789abcdef"
_CONVERSIONS = "https://api.github.com/app-manifests/the-code/conversions"
_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


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
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_credential_cipher] = lambda: cipher
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_start_returns_github_post_target_and_manifest(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession]
) -> None:
    r = await client.post("/api/v1/connectors/oauth/github/app-manifest/start")
    assert r.status_code == 200, r.text
    body = r.json()

    post_url = body["post_url"]
    assert post_url.startswith("https://github.com/settings/apps/new")
    state = parse_qs(urlsplit(post_url).query)["state"][0]
    assert state

    manifest = body["manifest"]
    issuer = get_settings().oauth_issuer.rstrip("/")
    assert manifest["redirect_url"] == (
        f"{issuer}/api/v1/connectors/oauth/github/app-manifest/callback"
    )
    assert manifest["callback_urls"] == [f"{issuer}/api/v1/connectors/oauth/github/callback"]
    # Not derived from the request host.
    assert "test" not in urlsplit(manifest["redirect_url"]).netloc

    # A pending row stashes the state under the manifest marker.
    async with sf() as s:
        row = await s.get(ConnectorOAuthPendingRow, state)
    assert row is not None
    assert row.provider == _MANIFEST_PENDING_PROVIDER


@respx.mock(assert_all_mocked=False)
async def test_callback_converts_code_persists_app_and_registers_provider(
    respx_mock: respx.MockRouter,
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
) -> None:
    respx_mock.post(_CONVERSIONS).mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 654321,
                "slug": "bsvibe-dev",
                "client_id": "Iv1.minted",
                "client_secret": "minted-secret",
                "pem": _PEM,
                "webhook_secret": "minted-wh",
                "html_url": "https://github.com/apps/bsvibe-dev",
            },
        )
    )

    start = await client.post("/api/v1/connectors/oauth/github/app-manifest/start")
    state = parse_qs(urlsplit(start.json()["post_url"]).query)["state"][0]

    cb = await client.get(
        "/api/v1/connectors/oauth/github/app-manifest/callback",
        params={"code": "the-code", "state": state},
    )
    assert cb.status_code in (302, 307), cb.text
    assert cb.headers["location"].startswith(get_settings().pwa_url)

    # App creds persisted (decryptable).
    async with sf() as s:
        creds = await get_app_credentials(s, provider="github", cipher=cipher)
        # Pending row consumed (single-use).
        pending = await s.get(ConnectorOAuthPendingRow, state)
    assert creds is not None
    assert creds.app_id == "654321"
    assert creds.client_id == "Iv1.minted"
    assert creds.client_secret == "minted-secret"
    assert creds.private_key_pem == _PEM
    assert creds.webhook_secret == "minted-wh"
    assert pending is None

    # Provider registered immediately — "Connect with GitHub" works now.
    prov = get_provider("github")
    assert isinstance(prov, GitHubAppProvider)
    assert prov.supports_service_token is True


async def test_callback_bad_state_400(client: httpx.AsyncClient) -> None:
    r = await client.get(
        "/api/v1/connectors/oauth/github/app-manifest/callback",
        params={"code": "x", "state": "nope"},
    )
    assert r.status_code == 400


async def test_app_status_not_configured(client: httpx.AsyncClient) -> None:
    # Nothing set up yet (isolated registry, no DB creds) → not configured.
    s0 = await client.get("/api/v1/connectors/oauth/github/app-status")
    assert s0.status_code == 200, s0.text
    assert s0.json()["configured"] is False


@respx.mock(assert_all_mocked=False)
async def test_app_status_configured_after_manifest(
    respx_mock: respx.MockRouter, client: httpx.AsyncClient
) -> None:
    respx_mock.post(_CONVERSIONS).mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 1,
                "slug": "bsvibe-dev",
                "client_id": "Iv1.x",
                "client_secret": "s",
                "pem": _PEM,
                "html_url": "https://github.com/apps/bsvibe-dev",
            },
        )
    )
    start = await client.post("/api/v1/connectors/oauth/github/app-manifest/start")
    state = parse_qs(urlsplit(start.json()["post_url"]).query)["state"][0]
    await client.get(
        "/api/v1/connectors/oauth/github/app-manifest/callback",
        params={"code": "the-code", "state": state},
    )

    status_resp = await client.get("/api/v1/connectors/oauth/github/app-status")
    body = status_resp.json()
    assert body["configured"] is True
    assert body["app_slug"] == "bsvibe-dev"
    assert body["html_url"] == "https://github.com/apps/bsvibe-dev"
