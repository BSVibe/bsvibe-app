"""Lift 1 — github OAuth connect, real backend wiring (GitHubAppProvider).

The Lift 0 endpoint e2e ran against the StubProvider; this runs the SAME real
router + DB wiring with the REAL GitHubAppProvider registered, mocking ONLY
GitHub's own HTTP (token exchange + /user) via respx pass-through. Proves the
end-to-end connect: start → authorize URL (no PKCE challenge for GitHub) →
callback exchanges the code against GitHub, fetches the @login, and persists an
encrypted token bound to a ``github`` connector_account — the credential the
re-wired delivery path then resolves.

Only auth + cipher are overridden (the established connectors harness); the
provider, store, and resolution layers are exercised for real
(mock-fixtures-hide-wiring-bugs).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.api.webhooks import get_credential_cipher
from backend.connectors.auth import providers as providers_mod
from backend.connectors.auth.db import ConnectorOAuthTokenRow
from backend.connectors.auth.github import GitHubAppProvider
from backend.connectors.auth.providers import register_provider
from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

TEST_KEY = b"0123456789abcdef0123456789abcdef"
_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 — endpoint URL
_USER_URL = "https://api.github.com/user"


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


@pytest.fixture
def github_provider() -> Iterator[None]:
    """Register a real GitHubAppProvider for the test, restore after."""
    snapshot = dict(providers_mod._REGISTRY)
    register_provider(
        GitHubAppProvider(client_id="Iv1.cid", client_secret="csecret")  # noqa: S106
    )
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
    github_provider: None,
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


def _state_from(authorize_url: str) -> str:
    return parse_qs(urlsplit(authorize_url).query)["state"][0]


async def test_start_builds_github_authorize_url(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/v1/connectors/oauth/github/start")
    assert r.status_code == 200, r.text
    url = r.json()["authorize_url"]
    assert url.startswith("https://github.com/login/oauth/authorize")
    q = parse_qs(urlsplit(url).query)
    assert q["client_id"] == ["Iv1.cid"]
    assert q["state"][0]
    # GitHub does not implement PKCE — no code_challenge on the wire.
    assert "code_challenge" not in q


@respx.mock(assert_all_mocked=False)
async def test_callback_connects_github_persists_token_and_login(
    respx_mock: respx.MockRouter,
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    respx_mock.post(_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "ghu_real_access",
                "refresh_token": "ghr_real",
                "expires_in": 28800,
                "scope": "repo",
                "token_type": "bearer",
            },
        )
    )
    respx_mock.get(_USER_URL).mock(return_value=httpx.Response(200, json={"login": "octocat"}))

    start = await client.post("/api/v1/connectors/oauth/github/start")
    state = _state_from(start.json()["authorize_url"])

    cb = await client.get(
        "/api/v1/connectors/oauth/github/callback",
        params={"code": "auth-code", "state": state},
    )
    assert cb.status_code in (302, 307), cb.text

    async with sf() as s:
        tok = (
            await s.execute(
                select(ConnectorOAuthTokenRow).where(ConnectorOAuthTokenRow.provider == "github")
            )
        ).scalar_one()
        assert cipher.decrypt(tok.access_token_ciphertext) == "ghu_real_access"
        assert tok.refresh_token_ciphertext is not None
        assert cipher.decrypt(tok.refresh_token_ciphertext) == "ghr_real"
        assert tok.account_label == "@octocat"

        acct = await s.get(ConnectorAccountRow, tok.connector_account_id)
        assert acct is not None
        assert acct.connector == "github"
        assert acct.workspace_id == workspace_id
