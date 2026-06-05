"""Slice 0.3 — connector OAuth start/callback endpoints (real-backend e2e).

Exercises the full connect dance against a real DB session + real router
wiring (only auth + cipher are overridden, per the established connectors
test harness) so the wiring bugs that dependency_overrides+pre-seeding hide
(mock-fixtures-hide-wiring-bugs) surface here:

* ``POST /api/v1/connectors/oauth/{provider}/start`` → mints CSRF state +
  PKCE, persists a pending row, returns the provider authorize URL built with
  a CONFIGURED redirect_uri (not the request host).
* ``GET /api/v1/connectors/oauth/{provider}/callback`` (public) → claims the
  pending row (single-use), exchanges the code, persists an encrypted token
  row linked to a connector_account, redirects back to the PWA.

Built against StubProvider — no real provider until Lift 1.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.api.webhooks import get_credential_cipher
from backend.config import get_settings
from backend.connectors.auth.db import ConnectorOAuthPendingRow, ConnectorOAuthTokenRow
from backend.connectors.db import ConnectorAccountRow  # noqa: F401 — register table
from backend.router.accounts.crypto import CredentialCipher

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

TEST_KEY = b"0123456789abcdef0123456789abcdef"
PROVIDER = "stub"  # the only provider registered in Lift 0


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(sf, cipher: CredentialCipher, workspace_id: uuid.UUID):
    app = create_app()

    async def _session():
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
    q = parse_qs(urlsplit(authorize_url).query)
    return q["state"][0]


# ── start ─────────────────────────────────────────────────────────────


async def test_start_returns_authorize_url(client: httpx.AsyncClient) -> None:
    r = await client.post(f"/api/v1/connectors/oauth/{PROVIDER}/start")
    assert r.status_code == 200, r.text
    url = r.json()["authorize_url"]
    q = parse_qs(urlsplit(url).query)
    assert q["state"][0]
    assert q["code_challenge"][0]


async def test_start_redirect_uri_is_configured_not_request_host(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(f"/api/v1/connectors/oauth/{PROVIDER}/start")
    url = r.json()["authorize_url"]
    redirect_uri = parse_qs(urlsplit(url).query)["redirect_uri"][0]
    issuer = get_settings().oauth_issuer
    # Built from configured backend base, never the inbound request host.
    assert redirect_uri.startswith(issuer)
    assert redirect_uri.endswith(f"/api/v1/connectors/oauth/{PROVIDER}/callback")
    assert "test" not in urlsplit(redirect_uri).netloc


async def test_start_persists_pending_row(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession]
) -> None:
    r = await client.post(f"/api/v1/connectors/oauth/{PROVIDER}/start")
    state = _state_from(r.json()["authorize_url"])
    async with sf() as s:
        row = await s.get(ConnectorOAuthPendingRow, state)
    assert row is not None
    assert row.provider == PROVIDER
    assert row.code_verifier  # PKCE verifier stashed server-side


async def test_start_unknown_provider_404(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/v1/connectors/oauth/not-registered/start")
    assert r.status_code == 404


# ── callback ──────────────────────────────────────────────────────────


async def test_callback_round_trip_persists_encrypted_token(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
) -> None:
    start = await client.post(f"/api/v1/connectors/oauth/{PROVIDER}/start")
    state = _state_from(start.json()["authorize_url"])

    cb = await client.get(
        f"/api/v1/connectors/oauth/{PROVIDER}/callback",
        params={"code": "auth-xyz", "state": state},
    )
    # Redirects the browser back to the PWA settings.
    assert cb.status_code in (302, 307), cb.text
    assert cb.headers["location"].startswith(get_settings().pwa_url)

    async with sf() as s:
        tok = (
            await s.execute(
                select(ConnectorOAuthTokenRow).where(ConnectorOAuthTokenRow.provider == PROVIDER)
            )
        ).scalar_one()
        # Stored encrypted — ciphertext is not the plaintext.
        assert tok.access_token_ciphertext != "stub-access-auth-xyz"
        assert cipher.decrypt(tok.access_token_ciphertext) == "stub-access-auth-xyz"
        # Linked to a real connector_account.
        acct = await s.get(ConnectorAccountRow, tok.connector_account_id)
        assert acct is not None

        # Pending row consumed (single-use).
        pending = await s.get(ConnectorOAuthPendingRow, state)
        assert pending is None


async def test_callback_unknown_state_400(client: httpx.AsyncClient) -> None:
    r = await client.get(
        f"/api/v1/connectors/oauth/{PROVIDER}/callback",
        params={"code": "x", "state": "nonexistent-state"},
    )
    assert r.status_code == 400


async def test_callback_state_is_single_use(client: httpx.AsyncClient) -> None:
    start = await client.post(f"/api/v1/connectors/oauth/{PROVIDER}/start")
    state = _state_from(start.json()["authorize_url"])
    first = await client.get(
        f"/api/v1/connectors/oauth/{PROVIDER}/callback",
        params={"code": "x", "state": state},
    )
    assert first.status_code in (302, 307)
    # Replay with the same state must fail (CSRF / replay defense).
    replay = await client.get(
        f"/api/v1/connectors/oauth/{PROVIDER}/callback",
        params={"code": "x", "state": state},
    )
    assert replay.status_code == 400
