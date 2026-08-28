"""RFC 8628 over HTTP — device authorization, polling, and browser approval.

The wire contract is what the CLI codes against, so the shapes and the error
codes matter as much as the state machine underneath: a device that cannot tell
`authorization_pending` from `expired_token` either gives up early or polls
forever.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.oauth_db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.config import get_settings
from backend.identity.db import UserRow
from backend.identity.oauth_keys import reset_signing_key_for_tests
from backend.identity.workspaces_db import WorkspaceRow

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

CLIENT_ID = "dcr-device-cli"


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator[Any]:
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    monkeypatch.setenv("BSVIBE_OAUTH_ISSUER", "http://test")
    get_settings.cache_clear()
    reset_signing_key_for_tests()
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()
    reset_signing_key_for_tests()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def seeded_user(db, workspace_id) -> AsyncIterator[UserRow]:
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t-ws"))
        user = UserRow(supabase_user_id="test-user", email="t@example.com")
        s.add(user)
        await s.commit()
        yield user


@pytest_asyncio.fixture
async def client(db, workspace_id, seeded_user) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[Any]:
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_current_user_row] = lambda: seeded_user

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _start(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.post(
        "/api/oauth/device_authorization",
        data={"client_id": CLIENT_ID, "scope": "mcp:read mcp:write mcp:admin"},
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _poll(client: httpx.AsyncClient, device_code: str) -> httpx.Response:
    return await client.post(
        "/api/oauth/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": CLIENT_ID,
        },
    )


# ---------------------------------------------------------------------------
# §3.2 device authorization response
# ---------------------------------------------------------------------------


async def test_device_authorization_returns_the_rfc_shape(client) -> None:
    body = await _start(client)
    assert set(body) >= {
        "device_code",
        "user_code",
        "verification_uri",
        "verification_uri_complete",
        "expires_in",
        "interval",
    }
    # The human opens this; it must be the PWA, not the API host.
    assert body["verification_uri"].endswith("/device")
    assert body["user_code"] in body["verification_uri_complete"]
    assert body["interval"] >= 1


async def test_device_authorization_is_public(client) -> None:
    """The device has no credential yet — that is the entire point."""
    r = await client.post("/api/oauth/device_authorization", data={"client_id": CLIENT_ID})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# §3.5 token endpoint answers
# ---------------------------------------------------------------------------


async def test_polling_before_approval_is_authorization_pending(client) -> None:
    started = await _start(client)
    r = await _poll(client, started["device_code"])
    assert r.status_code == 400
    assert r.json()["error"] == "authorization_pending"


async def test_polling_twice_immediately_is_slow_down(client) -> None:
    started = await _start(client)
    await _poll(client, started["device_code"])
    r = await _poll(client, started["device_code"])
    assert r.status_code == 400
    assert r.json()["error"] == "slow_down"


async def test_unknown_device_code_is_invalid_grant(client) -> None:
    r = await _poll(client, "not-a-real-device-code")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


async def test_missing_device_code_is_invalid_request(client) -> None:
    r = await client.post(
        "/api/oauth/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": CLIENT_ID,
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Approval from the browser, then the device picks it up
# ---------------------------------------------------------------------------


async def test_approved_device_code_yields_a_token_pair(client) -> None:
    started = await _start(client)

    approve = await client.post(
        "/api/v1/oauth/device/approve", json={"user_code": started["user_code"]}
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["scope"] == ["mcp:read", "mcp:write", "mcp:admin"]

    r = await _poll(client, started["device_code"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"].count(".") == 2
    assert body["refresh_token"]

    # Single use — a replay must not mint a second credential.
    replay = await _poll(client, started["device_code"])
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


async def test_denied_device_code_reports_access_denied(client) -> None:
    started = await _start(client)
    deny = await client.post(
        "/api/v1/oauth/device/approve",
        json={"user_code": started["user_code"], "approve": False},
    )
    assert deny.status_code == 200, deny.text

    r = await _poll(client, started["device_code"])
    assert r.status_code == 400
    assert r.json()["error"] == "access_denied"


async def test_approving_an_unknown_code_is_404(client) -> None:
    r = await client.post("/api/v1/oauth/device/approve", json={"user_code": "ZZZZ-ZZZZ"})
    assert r.status_code == 404


async def test_lookup_shows_what_is_being_approved_before_approving(client) -> None:
    """The consent screen must be able to name the scopes it is granting."""
    started = await _start(client)
    r = await client.get(f"/api/v1/oauth/device?user_code={started['user_code']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client_id"] == CLIENT_ID
    assert body["scope"] == ["mcp:read", "mcp:write", "mcp:admin"]
    assert body["status"] == "pending"
    assert "device_code" not in body  # never expose the device half


async def test_user_code_is_accepted_as_typed(client) -> None:
    """Lowercased, dash dropped — a human retyping must not be punished."""
    started = await _start(client)
    typed = started["user_code"].lower().replace("-", "")
    r = await client.post("/api/v1/oauth/device/approve", json={"user_code": typed})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def test_metadata_advertises_the_device_grant(client) -> None:
    body = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert "urn:ietf:params:oauth:grant-type:device_code" in body["grant_types_supported"]
    assert body["device_authorization_endpoint"].endswith("/api/oauth/device_authorization")
