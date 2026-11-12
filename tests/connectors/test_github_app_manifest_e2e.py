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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.api.v1.connector_oauth import _MANIFEST_PENDING_PROVIDER
from backend.api.webhooks import get_credential_cipher
from backend.config import get_settings
from backend.connectors.auth import providers as providers_mod
from backend.connectors.auth.app_credentials import get_app_credentials, upsert_app_credentials
from backend.connectors.auth.db import ConnectorOAuthPendingRow
from backend.connectors.auth.github import GitHubAppProvider
from backend.connectors.auth.providers import get_provider
from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import WorkspaceRow
from backend.router.accounts.crypto import CredentialCipher

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

TEST_KEY = b"0123456789abcdef0123456789abcdef"
_START = "/api/v1/connectors/oauth/github/app-manifest/start"
_CALLBACK = "/api/v1/connectors/oauth/github/app-manifest/callback"
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
    providers_mod._REGISTRY.pop("github", None)  # start unconfigured, whatever ran before
    try:
        yield
    finally:
        providers_mod._REGISTRY.clear()
        providers_mod._REGISTRY.update(snapshot)


@pytest_asyncio.fixture
async def seed_plan(
    sf: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID
) -> Callable[[str], Awaitable[None]]:
    async def _seed(plan: str) -> None:
        async with sf() as s, workspace_session_scope(s, workspace_id):
            s.add(WorkspaceRow(id=workspace_id, name="ws", plan=plan))
            await s.commit()

    return _seed


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
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    seed_plan: Callable[[str], Awaitable[None]],
) -> None:
    await seed_plan("operator")
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
    seed_plan: Callable[[str], Awaitable[None]],
) -> None:
    await seed_plan("operator")
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
    respx_mock: respx.MockRouter,
    client: httpx.AsyncClient,
    seed_plan: Callable[[str], Awaitable[None]],
) -> None:
    await seed_plan("operator")
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


_SEED = Callable[[str], Awaitable[None]]


def _state(start: httpx.Response) -> str:
    return parse_qs(urlsplit(start.json()["post_url"]).query)["state"][0]


async def _seed_creds(sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher) -> None:
    async with sf() as s:
        await upsert_app_credentials(
            s,
            provider="github",
            app_id="1",
            client_id="Iv1.old",
            client_secret="old-secret",
            private_key_pem=_PEM,
            app_slug="old",
            webhook_secret=None,
            html_url=None,
            cipher=cipher,
        )
        await s.commit()


async def test_start_rejects_non_operator_owner_403(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], seed_plan: _SEED
) -> None:
    await seed_plan("free")
    r = await client.post(_START)
    assert r.status_code == 403, r.text
    async with sf() as s:
        pending = (await s.execute(select(ConnectorOAuthPendingRow))).scalars().all()
    assert pending == []


async def test_start_over_existing_app_needs_replace(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    seed_plan: _SEED,
) -> None:
    await seed_plan("operator")
    await _seed_creds(sf, cipher)
    assert (await client.post(_START)).status_code == 409
    assert (await client.post(_START, params={"replace": "true"})).status_code == 200


@respx.mock(assert_all_mocked=False, assert_all_called=False)
async def test_callback_without_replace_keeps_existing_credentials(
    respx_mock: respx.MockRouter,
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    seed_plan: _SEED,
) -> None:
    await seed_plan("operator")
    route = respx_mock.post(_CONVERSIONS).mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 9,
                "slug": "new",
                "client_id": "Iv1.new",
                "client_secret": "new-secret",
                "pem": _PEM,
                "html_url": "https://github.com/apps/new",
            },
        )
    )
    # Started while unconfigured (no replace); credentials appear before the callback.
    state = _state(await client.post(_START))
    await _seed_creds(sf, cipher)

    r = await client.get(_CALLBACK, params={"code": "the-code", "state": state})
    assert r.status_code == 302
    assert "github_app_error" in r.headers["location"]
    assert not route.called
    async with sf() as s:
        creds = await get_app_credentials(s, provider="github", cipher=cipher)
    assert creds is not None
    assert creds.client_id == "Iv1.old"


@respx.mock(assert_all_mocked=False)
async def test_callback_with_replace_overwrites(
    respx_mock: respx.MockRouter,
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    seed_plan: _SEED,
) -> None:
    await seed_plan("operator")
    await _seed_creds(sf, cipher)
    respx_mock.post(_CONVERSIONS).mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 9,
                "slug": "new",
                "client_id": "Iv1.new",
                "client_secret": "new-secret",
                "pem": _PEM,
                "html_url": "https://github.com/apps/new",
            },
        )
    )
    state = _state(await client.post(_START, params={"replace": "true"}))
    r = await client.get(_CALLBACK, params={"code": "the-code", "state": state})
    assert r.status_code == 302
    async with sf() as s:
        creds = await get_app_credentials(s, provider="github", cipher=cipher)
    assert creds is not None
    assert creds.client_id == "Iv1.new"


@respx.mock(assert_all_mocked=False, assert_all_called=False)
async def test_unbound_callback_state_writes_nothing(
    respx_mock: respx.MockRouter,
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
    seed_plan: _SEED,
) -> None:
    await seed_plan("operator")
    route = respx_mock.post(_CONVERSIONS)
    async with sf() as s:
        s.add(
            ConnectorOAuthPendingRow(
                state="legacy",
                provider=_MANIFEST_PENDING_PROVIDER,
                workspace_id=workspace_id,
                code_verifier="-",
                redirect_uri="https://x",
            )
        )
        await s.commit()
    r = await client.get(_CALLBACK, params={"code": "the-code", "state": "legacy"})
    assert r.status_code == 403
    assert not route.called
    async with sf() as s:
        assert await get_app_credentials(s, provider="github", cipher=cipher) is None
