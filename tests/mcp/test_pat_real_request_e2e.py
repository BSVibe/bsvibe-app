"""PAT → `/mcp` end-to-end, over the real transport with no auth overrides.

Every other test in this feature proves one layer in isolation: the row is
written, the JWT verifies, the route returns 201, the React panel renders the
token once. All of them can pass while nothing actually honours a PAT on the
request path — that is the half-wired failure this file exists to rule out.

So the `/mcp` half runs with **zero `dependency_overrides`** and a **real**
`StreamableHTTPSessionManager` (the SDK task group entered exactly as
`mcp_lifespan` does it). The only thing handed in is the bearer token, and what
comes back is the genuine tool list produced by the genuine registry.

The mint half sends a real HS256 session JWT too — the PAT routes authenticate
themselves (`resolve_pat_principal`) rather than riding the v1 router's gate, so
there is nothing left worth overriding except the database handle.
"""

from __future__ import annotations

import base64
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy.ext.asyncio import async_sessionmaker

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.oauth_db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
from backend.api.deps import get_db_session
from backend.api.main import create_app
from backend.config import get_settings
from backend.identity.db import MembershipRow, UserRow
from backend.identity.oauth_keys import reset_signing_key_for_tests
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.server import build_registry, build_server
from backend.mcp.streamable_http import build_streamable_http_app
from backend.shared.authz.settings import get_settings as get_authz_settings

from .._support import db_engine

pytestmark = pytest.mark.asyncio

ISSUER = "http://test"
SESSION_SECRET = "e2e-session-secret"
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch) -> AsyncIterator[Any]:
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(b"0" * 32).decode(),
    )
    monkeypatch.setenv("BSVIBE_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("BSVIBE_KNOWLEDGE_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("USER_JWT_SECRET", SESSION_SECRET)
    monkeypatch.setenv("USER_JWT_ALGORITHM", "HS256")
    monkeypatch.delenv("USER_JWT_JWKS_URL", raising=False)
    get_settings.cache_clear()
    # authz Settings is @lru_cache(maxsize=1): whichever module touches it
    # first would otherwise pin its USER_JWT_SECRET for the whole process.
    get_authz_settings.cache_clear()
    reset_signing_key_for_tests()
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()
    get_authz_settings.cache_clear()
    reset_signing_key_for_tests()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def seeded_user(db, workspace_id) -> AsyncIterator[UserRow]:
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t-ws", region="us-1"))
        user = UserRow(supabase_user_id="test-user", email="t@example.com")
        s.add(user)
        await s.flush()
        s.add(MembershipRow(user_id=user.id, workspace_id=workspace_id, role="owner"))
        await s.commit()
        yield user


@pytest_asyncio.fixture
async def api(db, workspace_id, seeded_user) -> AsyncIterator[httpx.AsyncClient]:
    """The REST app, used ONLY to mint and revoke — not to serve `/mcp`."""
    app = create_app()

    async def _session() -> AsyncIterator[Any]:
        async with db() as s:
            yield s

    # Only the database handle is redirected; the PAT routes authenticate the
    # bearer themselves.
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {_session_jwt(seeded_user)}"},
    ) as c:
        yield c


def _session_jwt(user: UserRow) -> str:
    import time

    import jwt

    now = int(time.time())
    return jwt.encode(
        {
            "sub": user.supabase_user_id,
            "aud": "bsvibe",
            "iat": now,
            "exp": now + 3600,
        },
        SESSION_SECRET,
        algorithm="HS256",
    )


@contextlib.asynccontextmanager
async def mcp_client(session_factory: Any) -> AsyncIterator[httpx.AsyncClient]:
    """The real `/mcp` ASGI app — same construction as `mcp_lifespan`.

    No `dependency_overrides` exist here at all: this is not a FastAPI app. The
    bearer token is resolved by `backend.mcp.auth.resolve_principal_from_bearer`
    against the real database, and the tool list comes from the real registry.
    """
    server = build_server(session_factory=session_factory, registry=build_registry())
    manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)
    asgi = build_streamable_http_app(
        issuer=ISSUER, session_factory=session_factory, manager=manager
    )
    async with manager.run():
        transport = httpx.ASGITransport(app=asgi)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as c:
            yield c


async def _rpc(
    client: httpx.AsyncClient, token: str, method: str, params: dict[str, Any] | None = None
) -> httpx.Response:
    return await client.post(
        "/",
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


async def _mint_pat(api: httpx.AsyncClient, name: str = "mac-mini") -> str:
    r = await api.post(
        "/api/v1/oauth/pats", json={"name": name, "scope": ["mcp:read", "mcp:write"]}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["expires_at"] is None
    return str(body["token"])


async def test_pat_authenticates_mcp_and_lists_real_tools(db, api) -> None:
    """The load-bearing claim: a PAT actually gets you a working MCP session."""
    token = await _mint_pat(api)

    async with mcp_client(db) as mcp:
        init = await _rpc(
            mcp,
            token,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pat-e2e", "version": "0"},
            },
        )
        assert init.status_code == 200, init.text

        listed = await _rpc(mcp, token, "tools/list")
        assert listed.status_code == 200, listed.text

    tools = _tools_from(listed)
    names = {tool["name"] for tool in tools}
    # Assert PRESENCE, not merely "no error". An empty list is exactly what a
    # silently unauthenticated or unwired transport returns, and a floor of one
    # would still pass on a degenerate registry. The real surface is ~86 tools;
    # 20 is a floor that survives ordinary churn but not a collapse.
    assert len(tools) >= 20, f"tool surface collapsed to {len(tools)}: {sorted(names)}"
    assert "bsvibe_knowledge_search" in names
    assert "bsvibe_products_list" in names


async def test_revoked_pat_is_rejected_with_www_authenticate(db, api) -> None:
    """Revocation has to bite on the request path, not just flip a column."""
    token = await _mint_pat(api, name="doomed")
    pat_id = (await api.get("/api/v1/oauth/pats")).json()[0]["id"]

    async with mcp_client(db) as mcp:
        # It works before the revoke — otherwise the 401 below proves nothing.
        before = await _rpc(mcp, token, "tools/list")
        assert before.status_code == 200, before.text

        assert (await api.delete(f"/api/v1/oauth/pats/{pat_id}")).status_code == 204

        after = await _rpc(mcp, token, "tools/list")

    assert after.status_code == 401
    # RFC 9728 — the client needs the resource-metadata pointer to recover.
    assert "www-authenticate" in {k.lower() for k in after.headers}


async def test_unknown_bearer_is_rejected(db) -> None:
    """Sanity: the transport is not accepting anything that looks like a token."""
    async with mcp_client(db) as mcp:
        r = await _rpc(mcp, "not-a-real-token", "tools/list")
    assert r.status_code == 401


def _tools_from(response: httpx.Response) -> list[dict[str, Any]]:
    """Pull `result.tools` out of a JSON-RPC reply (JSON or SSE-framed)."""
    body = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        import json

        for line in body.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[len("data:") :].strip())
                if "result" in payload:
                    return list(payload["result"].get("tools") or [])
        raise AssertionError(f"no JSON-RPC result in SSE stream: {body}")
    return list(response.json()["result"].get("tools") or [])
