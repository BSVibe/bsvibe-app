"""Transport-level tests — Lift D2.

Two test surfaces here:

1. The Streamable HTTP ASGI shim — covers the 401 / WWW-Authenticate
   contract directly against the ASGI app, with a fake MCP manager,
   so we don't need the full lifespan task-group up.
2. The RFC 9728 ``oauth-protected-resource`` metadata — verified via the
   FastAPI app over httpx ASGITransport (no lifespan needed: a GET on a
   plain metadata route is independent of the MCP transport).
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.oauth_db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
from backend.api.deps import get_current_user
from backend.api.main import create_app
from backend.config import get_settings
from backend.identity.oauth_keys import reset_signing_key_for_tests
from backend.mcp.streamable_http import build_streamable_http_app

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch) -> AsyncIterator:
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(b"0" * 32).decode(),
    )
    monkeypatch.setenv("BSVIBE_OAUTH_ISSUER", "http://test")
    monkeypatch.setenv("BSVIBE_KNOWLEDGE_VAULT_ROOT", str(tmp_path / "vault"))
    get_settings.cache_clear()
    reset_signing_key_for_tests()
    async with db_engine() as (engine, _is_pg):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()
    reset_signing_key_for_tests()


# ---------------------------------------------------------------------------
# 1. RFC 9728 metadata via the real FastAPI app — no lifespan required.
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def fastapi_client(db) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_metadata_resource_url_uses_top_level_mcp(fastapi_client) -> None:
    r = await fastapi_client.get("/api/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.json()
    # D2 mounted MCP at /mcp (NOT /api/mcp) — Claude Code parses this URL
    # out of the resource-metadata document to construct the MCP endpoint.
    assert body["resource"].endswith("/mcp")
    assert not body["resource"].endswith("/api/mcp")
    assert "mcp:read" in body["scopes_supported"]
    assert "mcp:write" in body["scopes_supported"]
    assert "mcp:admin" in body["scopes_supported"]


# ---------------------------------------------------------------------------
# 2. Streamable HTTP ASGI shim — fake manager, real auth code path.
# ---------------------------------------------------------------------------
async def _collect_response(asgi_app, scope: dict, body: bytes = b"") -> dict:
    """Drive one ASGI HTTP request and return the captured response."""
    receive_calls = [
        {"type": "http.request", "body": body, "more_body": False},
    ]

    async def receive() -> dict:
        return receive_calls.pop(0) if receive_calls else {"type": "http.disconnect"}

    captured: dict = {"headers": {}, "body": b"", "status": None}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            for k, v in message.get("headers", []):
                captured["headers"][k.decode("latin-1").lower()] = v.decode("latin-1")
        elif message["type"] == "http.response.body":
            captured["body"] += message.get("body", b"")

    await asgi_app(scope, receive, send)
    return captured


def _http_scope(*, headers: list[tuple[bytes, bytes]]) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
    }


async def test_streamable_app_returns_401_without_token(db) -> None:
    asgi = build_streamable_http_app(
        issuer="http://test",
        session_factory=db,
        manager=AsyncMock(),
    )
    scope = _http_scope(headers=[(b"content-type", b"application/json")])
    resp = await _collect_response(
        asgi, scope, body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    )
    assert resp["status"] == 401
    www = resp["headers"].get("www-authenticate", "")
    assert "Bearer" in www
    assert "resource_metadata=" in www
    assert "oauth-protected-resource" in www
    assert "error=" in www


async def test_streamable_app_returns_401_with_malformed_bearer(db) -> None:
    asgi = build_streamable_http_app(
        issuer="http://test",
        session_factory=db,
        manager=AsyncMock(),
    )
    scope = _http_scope(
        headers=[
            (b"authorization", b"Bearer not-a-real-jwt"),
            (b"content-type", b"application/json"),
        ],
    )
    resp = await _collect_response(asgi, scope, body=b"{}")
    assert resp["status"] == 401


async def test_streamable_app_returns_401_when_authorization_header_lacks_bearer(db) -> None:
    asgi = build_streamable_http_app(
        issuer="http://test",
        session_factory=db,
        manager=AsyncMock(),
    )
    scope = _http_scope(
        headers=[(b"authorization", b"Basic dXNlcjpwYXNz")],
    )
    resp = await _collect_response(asgi, scope, body=b"{}")
    assert resp["status"] == 401


async def test_streamable_app_delegates_to_manager_when_token_valid(db, monkeypatch) -> None:
    """A verified principal hands the request to the SDK manager unchanged."""
    import time
    from datetime import datetime, timedelta

    from backend.identity.db import UserRow
    from backend.identity.oauth_db import OAuthAccessTokenRow
    from backend.identity.oauth_jwt import issue_access_token
    from backend.identity.oauth_keys import get_signing_key
    from backend.identity.workspaces_db import WorkspaceRow

    jti = uuid.uuid4()
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        s.add(UserRow(id=user_id, supabase_user_id="t", email="t@e.co"))
        await s.flush()
        now = datetime.now(UTC)
        s.add(
            OAuthAccessTokenRow(
                id=jti,
                workspace_id=workspace_id,
                user_id=user_id,
                client_id="dcr-test",
                scope=["mcp:read"],
                issued_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        await s.commit()
    token = issue_access_token(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scope=["mcp:read"],
        jti=jti,
        issued_at=int(time.time()),
        expires_at=int(time.time()) + 3600,
        issuer="http://test",
        signing_key=get_signing_key(),
    )

    manager = AsyncMock()

    async def fake_handle(scope, receive, send):
        # Stand in for the SDK — write a 200 OK so the test can observe
        # delegation success.
        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"x-served", b"manager")]}
        )
        await send({"type": "http.response.body", "body": b'{"ok": true}'})

    manager.handle_request = fake_handle

    asgi = build_streamable_http_app(
        issuer="http://test",
        session_factory=db,
        manager=manager,
    )
    scope = _http_scope(
        headers=[
            (b"authorization", f"Bearer {token}".encode("ascii")),
            (b"content-type", b"application/json"),
        ],
    )
    resp = await _collect_response(asgi, scope, body=b"{}")
    assert resp["status"] == 200
    assert resp["headers"].get("x-served") == "manager"
