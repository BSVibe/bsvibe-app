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
    r = await client.get("/.well-known/oauth-authorization-server")
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
    r = await client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.json()
    # D2 mounted the MCP server at /mcp (NOT /api/mcp) — top-level path
    # so MCP clients construct a clean server URL.
    assert body["resource"].endswith("/mcp")
    assert not body["resource"].endswith("/api/mcp")
    assert "mcp:read" in body["scopes_supported"]


async def test_jwks_returns_es256(client: httpx.AsyncClient) -> None:
    r = await client.get("/.well-known/jwks.json")
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
    # Listing hides revoked rows (Settings UI noise reduction); the row
    # stays in the table for audit + FK integrity but is filtered out.
    listed = (await client.get("/api/v1/oauth/clients")).json()
    assert not any(c["client_id"] == client_id for c in listed)


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


async def test_anon_register_accepts_rfc7591_full_metadata(
    client: httpx.AsyncClient,
) -> None:
    """Claude Code / MCP Inspector send the full RFC 7591 §2 vocabulary."""
    r = await client.post(
        "/api/oauth/register",
        json={
            "client_name": "Claude Code",
            "redirect_uris": ["http://127.0.0.1:54321/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": "mcp:read mcp:write",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["client_id"].startswith("dcr-")
    assert sorted(body["allowed_scopes"]) == ["mcp:read", "mcp:write"]


async def test_anon_register_rejects_unsupported_grant_type(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/api/oauth/register",
        json={
            "client_name": "x",
            "redirect_uris": ["http://127.0.0.1/cb"],
            "grant_types": ["client_credentials"],
        },
    )
    assert r.status_code == 422


async def test_anon_register_rejects_token_endpoint_auth_method_other_than_none(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/api/oauth/register",
        json={
            "client_name": "x",
            "redirect_uris": ["http://127.0.0.1/cb"],
            "token_endpoint_auth_method": "client_secret_basic",
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
    r = await client.get("/.well-known/oauth-authorization-server")
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


async def test_authorize_get_redirects_to_pwa_consent(client: httpx.AsyncClient) -> None:
    """A valid GET /authorize 302s to the PWA-hosted consent page.

    Browser top-level navigations can't carry a Bearer header, so the
    consent screen lives in the PWA (where the Supabase session is
    reachable). This endpoint is auth-free + just a redirector.
    """
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
        follow_redirects=False,
    )
    assert r.status_code == 302
    location = r.headers["location"]
    # In tests BSVIBE_PWA_URL is unset → default ``http://localhost:3700``.
    assert location.startswith("http://localhost:3700/oauth/consent?")
    assert f"client_id={reg['client_id']}" in location
    assert "scope=mcp%3Aread" in location
    assert "state=xyz" in location
    assert f"code_challenge={CHALLENGE}" in location


async def test_authorize_get_preserves_loopback_redirect_uri(
    client: httpx.AsyncClient,
) -> None:
    """GET /authorize preserves ``redirect_uri`` into the PWA consent URL.

    Lift E11 regression guard. The CLI loopback flow opens this endpoint with
    ``redirect_uri=http://127.0.0.1:<port>/`` — that exact value must round-
    trip through the consent screen so when the founder clicks "Allow", the
    backend's POST /authorize returns ``redirect_to`` pointing at the right
    loopback port. A drop here means the browser navigates to /brief and the
    CLI's `_wait_for_callback` server times out at 300s.
    """
    reg = await _register(client)
    # Path MUST match the registered ``/callback`` (RFC 8252 §7.3 lets the
    # port vary on loopback, but path is still strict-match). The real CLI
    # uses ``http://127.0.0.1:<port>/`` because it registers a fresh DCR
    # client whose redirect_uris list ALSO ends in ``/`` — the registered
    # path always matches the requested one. Tests can't repeat the DCR
    # round-trip cheaply, so we mirror the path the fixture client carries.
    loopback = "http://127.0.0.1:53113/callback"
    r = await client.get(
        "/api/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": loopback,
            "scope": "mcp:read mcp:write",
            "state": "round-trip-state",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    location = r.headers["location"]
    # Parse the consent URL and verify every required OAuth param is present
    # and the redirect_uri matches the loopback URI exactly. urllib parsing
    # decodes the percent-encoding so we compare the canonical value.
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(location)
    assert parts.path == "/oauth/consent"
    qs = parse_qs(parts.query)
    assert qs.get("redirect_uri") == [loopback], f"missing redirect_uri in {qs=} location={location}"
    assert qs["client_id"] == [reg["client_id"]]
    assert qs["response_type"] == ["code"]
    assert qs["scope"] == ["mcp:read mcp:write"]
    assert qs["state"] == ["round-trip-state"]
    assert qs["code_challenge"] == [CHALLENGE]
    assert qs["code_challenge_method"] == ["S256"]


async def test_authorize_unknown_client_redirects_to_consent_with_error(
    client: httpx.AsyncClient,
) -> None:
    """Unknown client_id → 302 to PWA consent with ``?error=invalid_client``.

    The PWA renders a clean "unknown client" UI instead of a JSON 400.
    """
    r = await client.get(
        "/api/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "dcr-unknown",
            "redirect_uri": "http://127.0.0.1/cb",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("http://localhost:3700/oauth/consent?")
    assert "error=invalid_client" in location


async def test_authorize_get_is_auth_free(client: httpx.AsyncClient) -> None:
    """No Bearer + no test dependency_overrides: GET /authorize still 302s.

    The browser navigation that opens this endpoint cannot carry an
    Authorization header — that's the whole reason this lift exists.
    Validates the endpoint never trips a 401/403 path.
    """
    reg = await _register(client)
    # Build a NEW transport with NO test-fixture session bearer attached,
    # mirroring what an actual browser navigation produces.
    r = await client.get(
        "/api/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://127.0.0.1:54321/callback",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
        headers={"authorization": ""},  # explicitly drop any harness Authorization
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("http://localhost:3700/oauth/consent?")


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
    URI ``http://127.0.0.1:54321/cb``. Validates by reaching the PWA
    consent redirect (vs the ``invalid_request`` error path)."""
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
        follow_redirects=False,
    )
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("http://localhost:3700/oauth/consent?")
    assert "error=" not in location


async def test_authorize_rejects_non_loopback_http(
    client: httpx.AsyncClient,
) -> None:
    """Unknown redirect_uri → 302 back to PWA consent with ``?error=…``.

    Same pattern as ``test_authorize_unknown_client_redirects_to_consent_with_error``
    — we never bounce a request to an unverified redirect_uri (would be
    an open redirect), so the PWA renders the error.
    """
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
        follow_redirects=False,
    )
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("http://localhost:3700/oauth/consent?")
    assert "error=invalid_request" in location


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


# ---------------------------------------------------------------------------
# POST /authorize JSON shape — PWA consent fetch
# ---------------------------------------------------------------------------


async def test_authorize_post_json_approve_returns_redirect_to(
    client: httpx.AsyncClient,
) -> None:
    """PWA-style POST (``Accept: application/json``) returns JSON, not 302.

    A cross-origin browser ``fetch`` to the API origin can't follow a
    302 to ``http://localhost:49921/callback`` — the JS has to do
    ``window.location.href = response.redirect_to``. Hence this shape.
    """
    reg = await _register(client)
    r = await client.post(
        "/api/oauth/authorize",
        headers={"accept": "application/json"},
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
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"redirect_to"}
    assert body["redirect_to"].startswith("http://127.0.0.1:54321/callback")
    assert "code=" in body["redirect_to"]
    assert "state=xyz" in body["redirect_to"]


async def test_authorize_post_json_deny_returns_access_denied(
    client: httpx.AsyncClient,
) -> None:
    """``action=deny`` + JSON-Accept returns the access_denied bounce URL."""
    reg = await _register(client)
    r = await client.post(
        "/api/oauth/authorize",
        headers={"accept": "application/json"},
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
    )
    assert r.status_code == 200
    body = r.json()
    assert "error=access_denied" in body["redirect_to"]
    assert "state=xyz" in body["redirect_to"]


async def test_authorize_post_unknown_action_400(client: httpx.AsyncClient) -> None:
    """An action other than approve/deny is a hard 400 (programmer error)."""
    reg = await _register(client)
    r = await client.post(
        "/api/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "http://127.0.0.1:54321/callback",
            "scope": "mcp:read",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "action": "nope",
        },
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/oauth/clients/by-client-id/{client_id} — PWA consent fetch
# ---------------------------------------------------------------------------


async def test_public_client_lookup_returns_metadata(client: httpx.AsyncClient) -> None:
    """The PWA consent page fetches this to render "Allow {name}…"."""
    reg = await _register(client)
    r = await client.get(f"/api/oauth/clients/by-client-id/{reg['client_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "client_id": reg["client_id"],
        "client_name": "Claude Code",
        "client_type": "public",
        "redirect_uris": ["http://127.0.0.1/callback"],
        "allowed_scopes": ["mcp:read", "mcp:write"],
    }


async def test_public_client_lookup_unknown_returns_404(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/api/oauth/clients/by-client-id/dcr-nope")
    assert r.status_code == 404


async def test_public_client_lookup_revoked_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """A revoked client is hidden — don't disclose it sat in a soft state."""
    reg = await _register(client)
    await client.delete(f"/api/v1/oauth/clients/{reg['client_id']}")
    r = await client.get(f"/api/oauth/clients/by-client-id/{reg['client_id']}")
    assert r.status_code == 404
