"""End-to-end API tests for the embedded OAuth server — Lift D1.

Drives the full RFC 6749 authorization_code + PKCE flow through the
FastAPI app: register client → /authorize POST (approve) → /token →
/introspect → /revoke. The Supabase auth dependency is stubbed via
``fake_current_user``; everything below the auth boundary runs for real.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import AsyncIterator

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


VERIFIER = "abcDEF123-._~abcDEF123-._~abcDEF123-._~xyzAB"


def _challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


CHALLENGE = _challenge_for(VERIFIER)


@pytest_asyncio.fixture
async def db(monkeypatch):
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(b"0" * 32).decode(),
    )
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
    """Seed a workspace + a UserRow whose Supabase id matches fake_current_user."""
    async with db() as s:
        ws = WorkspaceRow(id=workspace_id, name="t-ws", region="us-1")
        s.add(ws)
        user = UserRow(supabase_user_id="test-user", email="t@example.com")
        s.add(user)
        await s.commit()
        yield user


@pytest_asyncio.fixture
async def client(db, workspace_id, seeded_user) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    # The /authorize endpoint resolves the UserRow; make it return our seeded user.
    def _user_row() -> UserRow:
        return seeded_user

    app.dependency_overrides[get_current_user_row] = _user_row

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# .well-known + JWKS
# ---------------------------------------------------------------------------


async def test_oauth_authorization_server_metadata(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    body = r.json()
    assert body["issuer"] == "http://test"
    assert body["authorization_endpoint"].endswith("/api/oauth/authorize")
    assert body["token_endpoint"].endswith("/api/oauth/token")
    assert "S256" in body["code_challenge_methods_supported"]
    assert "authorization_code" in body["grant_types_supported"]
    assert "refresh_token" in body["grant_types_supported"]
    assert "mcp:read" in body["scopes_supported"]


async def test_oauth_protected_resource_metadata(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.json()
    # D2 mounted the MCP server at /mcp (NOT /api/mcp) — top-level path
    # so MCP clients construct a clean server URL.
    assert body["resource"].endswith("/mcp")
    assert not body["resource"].endswith("/api/mcp")
    assert "mcp:read" in body["scopes_supported"]


async def test_jwks_returns_es256(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/.well-known/jwks.json")
    assert r.status_code == 200
    body = r.json()
    assert "keys" in body
    assert body["keys"][0]["alg"] == "ES256"
    assert body["keys"][0]["crv"] == "P-256"


# ---------------------------------------------------------------------------
# DCR client management
# ---------------------------------------------------------------------------


async def test_register_client_returns_client_id(client: httpx.AsyncClient) -> None:
    payload = {
        "client_name": "Claude Code",
        "redirect_uris": ["http://127.0.0.1/callback"],
        "allowed_scopes": ["mcp:read", "mcp:write"],
    }
    r = await client.post("/api/v1/oauth/clients", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["client_id"].startswith("dcr-")
    assert body["client_name"] == "Claude Code"
    assert body["client_type"] == "public"


async def test_list_clients_returns_just_created(client: httpx.AsyncClient) -> None:
    await client.post(
        "/api/v1/oauth/clients",
        json={
            "client_name": "Test-A",
            "redirect_uris": ["http://127.0.0.1/cb"],
        },
    )
    r = await client.get("/api/v1/oauth/clients")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["client_name"] == "Test-A"


async def test_delete_client_marks_revoked(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/oauth/clients",
        json={"client_name": "Doomed", "redirect_uris": ["http://127.0.0.1/cb"]},
    )
    client_id = r.json()["client_id"]
    r2 = await client.delete(f"/api/v1/oauth/clients/{client_id}")
    assert r2.status_code == 204
    # List still includes it but revoked_at is set.
    listed = (await client.get("/api/v1/oauth/clients")).json()
    assert any(c["client_id"] == client_id and c["revoked_at"] for c in listed)


async def test_register_client_rejects_http_external_host(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/api/v1/oauth/clients",
        json={"client_name": "Bad", "redirect_uris": ["http://evil.com/cb"]},
    )
    assert r.status_code == 400


async def test_register_client_rejects_unknown_scope(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/api/v1/oauth/clients",
        json={
            "client_name": "Bad",
            "redirect_uris": ["http://127.0.0.1/cb"],
            "allowed_scopes": ["mcp:read", "evil:write"],
        },
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# MCP no-slash redirect — Lift D2 hotfix
# ---------------------------------------------------------------------------


async def test_mcp_no_slash_redirects_to_trailing_slash(
    client: httpx.AsyncClient,
) -> None:
    # 307 preserves method + body so the MCP transport eventually handles it.
    r = await client.post("/mcp", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/mcp/"


async def test_mcp_no_slash_preserves_query_string(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/mcp?token=foo", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/mcp/?token=foo"


# ---------------------------------------------------------------------------
# Anonymous DCR (RFC 7591 §3 open) — Lift D2 followup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_anon_dcr_buckets():
    """Clear the in-process anonymous-DCR rate-limit table between tests."""
    from backend.api import oauth as oauth_mod

    oauth_mod._anon_dcr_buckets.clear()
    yield
    oauth_mod._anon_dcr_buckets.clear()


async def test_anon_register_loopback_succeeds(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/oauth/register",
        json={
            "client_name": "Claude Code",
            "redirect_uris": ["http://127.0.0.1:54321/callback"],
            "allowed_scopes": ["mcp:read"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["client_id"].startswith("dcr-")
    assert body["client_name"] == "Claude Code"
    assert "client_secret" not in body


async def test_anon_register_rejects_external_host(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/oauth/register",
        json={
            "client_name": "Phish",
            "redirect_uris": ["http://evil.com/callback"],
        },
    )
    assert r.status_code == 422


async def test_anon_register_rejects_https_redirect(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/oauth/register",
        json={
            "client_name": "Web",
            "redirect_uris": ["https://example.com/callback"],
        },
    )
    assert r.status_code == 422


async def test_anon_register_rejects_unknown_scope(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/oauth/register",
        json={
            "client_name": "X",
            "redirect_uris": ["http://localhost/cb"],
            "allowed_scopes": ["mcp:read", "root:owner"],
        },
    )
    assert r.status_code == 422


async def test_anon_register_rate_limit_kicks_in(client: httpx.AsyncClient) -> None:
    body = {
        "client_name": "burst",
        "redirect_uris": ["http://127.0.0.1/cb"],
    }
    # 10 successful registrations consume the bucket.
    for _ in range(10):
        r = await client.post("/api/oauth/register", json=body)
        assert r.status_code == 201, r.text
    r = await client.post("/api/oauth/register", json=body)
    assert r.status_code == 429


async def test_metadata_registration_endpoint_is_open_dcr(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/api/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    meta = r.json()
    # Open DCR endpoint must be advertised, NOT the founder-authed v1 route.
    assert meta["registration_endpoint"].endswith("/api/oauth/register")


# ---------------------------------------------------------------------------
# Authorization-code flow end-to-end
# ---------------------------------------------------------------------------


async def _register(client: httpx.AsyncClient) -> dict:
    r = await client.post(
        "/api/v1/oauth/clients",
        json={
            "client_name": "Claude Code",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "allowed_scopes": ["mcp:read", "mcp:write"],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_authorize_get_renders_consent(client: httpx.AsyncClient) -> None:
    reg = await _register(client)
    r = await client.get(
        "/api/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://127.0.0.1:54321/callback",
            "scope": "mcp:read",
            "state": "xyz",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code == 200
    assert "Authorize" in r.text
    assert "mcp:read" in r.text


async def test_authorize_unknown_client(client: httpx.AsyncClient) -> None:
    r = await client.get(
        "/api/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "dcr-unknown",
            "redirect_uri": "http://127.0.0.1/cb",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code == 400
    assert "OAuth error" in r.text


async def test_authorize_post_approve_redirects_with_code(
    client: httpx.AsyncClient,
) -> None:
    reg = await _register(client)
    r = await client.post(
        "/api/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://127.0.0.1:54321/callback",
            "scope": "mcp:read",
            "state": "xyz",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "action": "approve",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("http://127.0.0.1:54321/callback")
    assert "code=" in location
    assert "state=xyz" in location


async def test_authorize_post_deny_redirects_with_error(
    client: httpx.AsyncClient,
) -> None:
    reg = await _register(client)
    r = await client.post(
        "/api/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://127.0.0.1:54321/callback",
            "scope": "mcp:read",
            "state": "xyz",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "action": "deny",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=access_denied" in r.headers["location"]


async def test_full_authorization_code_flow(client: httpx.AsyncClient) -> None:
    reg = await _register(client)
    # 1. /authorize approve → 302 with code
    approve = await client.post(
        "/api/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://127.0.0.1:54321/callback",
            "scope": "mcp:read mcp:write",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "action": "approve",
        },
        follow_redirects=False,
    )
    assert approve.status_code == 302
    location = approve.headers["location"]
    # Pull ``code`` out of the redirect.
    from urllib.parse import parse_qs, urlsplit

    code = parse_qs(urlsplit(location).query)["code"][0]

    # 2. /token authorization_code grant
    token_resp = await client.post(
        "/api/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": reg["client_id"],
            "code": code,
            "redirect_uri": "http://127.0.0.1:54321/callback",
            "code_verifier": VERIFIER,
        },
    )
    assert token_resp.status_code == 200, token_resp.text
    body = token_resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["scope"] == "mcp:read mcp:write"
    assert body["access_token"].count(".") == 2
    assert body["refresh_token"]
    access = body["access_token"]
    refresh = body["refresh_token"]

    # 3. /introspect → active
    intro = await client.post("/api/oauth/introspect", data={"token": access})
    assert intro.status_code == 200
    intro_body = intro.json()
    assert intro_body["active"] is True
    assert intro_body["scope"] == "mcp:read mcp:write"
    assert intro_body["client_id"] == reg["client_id"]

    # 4. /token refresh_token grant
    refresh_resp = await client.post(
        "/api/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": reg["client_id"],
            "refresh_token": refresh,
        },
    )
    assert refresh_resp.status_code == 200, refresh_resp.text
    new_access = refresh_resp.json()["access_token"]
    assert new_access != access

    # 5. /revoke the new access token
    rev = await client.post("/api/oauth/revoke", data={"token": new_access})
    assert rev.status_code == 200
    intro2 = await client.post("/api/oauth/introspect", data={"token": new_access})
    assert intro2.json()["active"] is False


async def test_token_replay_returns_invalid_grant(
    client: httpx.AsyncClient,
) -> None:
    reg = await _register(client)
    approve = await client.post(
        "/api/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://127.0.0.1:54321/callback",
            "scope": "mcp:read",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "action": "approve",
        },
        follow_redirects=False,
    )
    from urllib.parse import parse_qs, urlsplit

    code = parse_qs(urlsplit(approve.headers["location"]).query)["code"][0]
    form = {
        "grant_type": "authorization_code",
        "client_id": reg["client_id"],
        "code": code,
        "redirect_uri": "http://127.0.0.1:54321/callback",
        "code_verifier": VERIFIER,
    }
    r1 = await client.post("/api/oauth/token", data=form)
    assert r1.status_code == 200
    r2 = await client.post("/api/oauth/token", data=form)
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"


async def test_token_bad_pkce_verifier(client: httpx.AsyncClient) -> None:
    reg = await _register(client)
    approve = await client.post(
        "/api/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://127.0.0.1:54321/callback",
            "scope": "mcp:read",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "action": "approve",
        },
        follow_redirects=False,
    )
    from urllib.parse import parse_qs, urlsplit

    code = parse_qs(urlsplit(approve.headers["location"]).query)["code"][0]
    r = await client.post(
        "/api/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": reg["client_id"],
            "code": code,
            "redirect_uri": "http://127.0.0.1:54321/callback",
            "code_verifier": "A" * 43,  # wrong
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


async def test_authorize_redirect_uri_loopback_port_flex(
    client: httpx.AsyncClient,
) -> None:
    """RFC 8252 §7.3 — registered ``http://127.0.0.1/cb`` matches request
    URI ``http://127.0.0.1:54321/cb``."""
    reg = await _register(client)
    r = await client.get(
        "/api/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://127.0.0.1:54321/callback",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code == 200


async def test_authorize_rejects_non_loopback_http(
    client: httpx.AsyncClient,
) -> None:
    reg = await _register(client)
    r = await client.get(
        "/api/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://evil.com/callback",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code == 400


async def test_authorize_requires_pkce(client: httpx.AsyncClient) -> None:
    reg = await _register(client)
    r = await client.get(
        "/api/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://127.0.0.1:54321/callback",
        },
        follow_redirects=False,
    )
    # redirect_uri is known-good → 302 back with error
    assert r.status_code == 302
    assert "error=invalid_request" in r.headers["location"]


async def test_unsupported_grant_type(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/oauth/token",
        data={"grant_type": "password", "client_id": "x"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"
