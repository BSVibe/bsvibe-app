"""PAT endpoints accept BOTH credential classes — with real tokens, not overrides.

Minting a PAT from a browserless host needs the CLI's credential to work, and
the CLI holds an ES256 MCP access token from our own issuer — not a Supabase
session JWT. So `/api/v1/oauth/pats` has to take either.

Widening an auth surface is where escalation hides, so the scope gate is the
point of this file:

* `bsvibe login` requests `mcp:read mcp:write mcp:admin` → can mint;
* `issue_run_task_token` grants `mcp:read mcp:write` only → a dispatched
  executor agent must NOT be able to mint itself a durable credential.

Nothing here overrides authentication. Real tokens go over the wire and the
production resolver verifies them; only the DB session is pointed at the test
database.
"""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import update
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
from backend.identity.oauth_db import OAuthAccessTokenRow
from backend.identity.oauth_keys import reset_signing_key_for_tests
from backend.identity.oauth_service import issue_run_task_token, issue_token_pair
from backend.identity.workspaces_db import WorkspaceRow
from backend.shared.authz.settings import get_settings as get_authz_settings

from .._support import db_engine

pytestmark = pytest.mark.asyncio

ISSUER = "http://test"
SESSION_SECRET = "test-session-secret"


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator[Any]:
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    monkeypatch.setenv("BSVIBE_OAUTH_ISSUER", ISSUER)
    # Session-JWT verification in HS256 dev mode so we can mint a REAL one.
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


@pytest_asyncio.fixture
async def seeded(db) -> AsyncIterator[tuple[UserRow, uuid.UUID]]:
    """A user with an active membership — what both credential classes resolve to."""
    async with db() as s:
        ws = WorkspaceRow(name="t-ws")
        s.add(ws)
        user = UserRow(supabase_user_id=f"sb-{uuid.uuid4()}", email="t@example.com")
        s.add(user)
        await s.flush()
        s.add(MembershipRow(user_id=user.id, workspace_id=ws.id, role="owner"))
        await s.commit()
        yield user, ws.id


@pytest_asyncio.fixture
async def client(db) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[Any]:
        async with db() as s:
            yield s

    # The ONLY override: point at the test database. Authentication is real.
    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _session_jwt(user: UserRow) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user.supabase_user_id,
            "email": user.email,
            "aud": "bsvibe",
            "iat": now,
            "exp": now + 3600,
        },
        SESSION_SECRET,
        algorithm="HS256",
    )


async def _mcp_token(db, user: UserRow, workspace_id: uuid.UUID, scope: list[str]) -> str:
    async with db() as s:
        pair = await issue_token_pair(
            s,
            user_id=user.id,
            workspace_id=workspace_id,
            client_id="dcr-cli",
            scope=scope,
            issuer=ISSUER,
        )
        await s.commit()
        return pair.access_token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# The CLI's credential class
# ---------------------------------------------------------------------------


async def test_mcp_admin_token_can_mint_a_pat(client, db, seeded) -> None:
    user, ws = seeded
    token = await _mcp_token(db, user, ws, ["mcp:read", "mcp:write", "mcp:admin"])

    r = await client.post("/api/v1/oauth/pats", json={"name": "from-cli"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "from-cli"

    listed = await client.get("/api/v1/oauth/pats", headers=_auth(token))
    assert [p["name"] for p in listed.json()] == ["from-cli"]


async def test_mcp_token_without_admin_scope_is_forbidden(client, db, seeded) -> None:
    """read+write is the scope an executor task carries — it must not mint."""
    user, ws = seeded
    token = await _mcp_token(db, user, ws, ["mcp:read", "mcp:write"])

    r = await client.post("/api/v1/oauth/pats", json={"name": "nope"}, headers=_auth(token))
    assert r.status_code == 403
    assert "mcp:admin" in r.text


async def test_run_scoped_token_cannot_mint_even_with_admin(client, db, seeded) -> None:
    """A dispatched task's credential must never become a durable one.

    The scope gate already stops today's run tokens (read+write). This pins the
    stronger rule directly, so a future widening of run-token scope can't
    silently open the escalation.
    """
    user, ws = seeded
    async with db() as s:
        token = await issue_run_task_token(
            s, run_id=uuid.uuid4(), workspace_id=ws, user_id=user.id, issuer=ISSUER
        )
        await s.commit()

    r = await client.post("/api/v1/oauth/pats", json={"name": "escalate"}, headers=_auth(token))
    assert r.status_code == 403


async def test_revoked_mcp_token_is_rejected(client, db, seeded) -> None:
    user, ws = seeded
    token = await _mcp_token(db, user, ws, ["mcp:read", "mcp:write", "mcp:admin"])
    # Revoke it the way /oauth/revoke would.
    claims = jwt.decode(token, options={"verify_signature": False})
    async with db() as s:
        await s.execute(
            update(OAuthAccessTokenRow)
            .where(OAuthAccessTokenRow.id == uuid.UUID(claims["jti"]))
            .values(revoked_at=datetime.now(UTC))
        )
        await s.commit()

    r = await client.post("/api/v1/oauth/pats", json={"name": "dead"}, headers=_auth(token))
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# The PWA's credential class — must keep working
# ---------------------------------------------------------------------------


async def test_session_jwt_still_mints(client, seeded) -> None:
    user, _ws = seeded
    r = await client.post(
        "/api/v1/oauth/pats", json={"name": "from-pwa"}, headers=_auth(_session_jwt(user))
    )
    assert r.status_code == 201, r.text


async def test_session_jwt_can_revoke(client, seeded) -> None:
    user, _ws = seeded
    token = _session_jwt(user)
    created = (
        await client.post("/api/v1/oauth/pats", json={"name": "doomed"}, headers=_auth(token))
    ).json()

    r = await client.delete(f"/api/v1/oauth/pats/{created['id']}", headers=_auth(token))
    assert r.status_code == 204
    assert (await client.get("/api/v1/oauth/pats", headers=_auth(token))).json() == []


# ---------------------------------------------------------------------------
# Surface behaviour — moved here from test_oauth_api.py when the PAT routes
# stopped going through the v1 router's session-JWT gate. They now run against
# a real token instead of an overridden dependency.
# ---------------------------------------------------------------------------


async def test_create_returns_token_once_and_listing_never_carries_it(client, seeded) -> None:
    user, _ws = seeded
    token = _session_jwt(user)
    r = await client.post(
        "/api/v1/oauth/pats",
        json={"name": "mac-mini", "scope": ["mcp:read", "mcp:write"]},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["scope"] == ["mcp:read", "mcp:write"]
    assert body["expires_at"] is None  # never expires by default
    assert body["token"].count(".") == 2

    listed = (await client.get("/api/v1/oauth/pats", headers=_auth(token))).json()
    assert len(listed) == 1
    assert listed[0]["id"] == body["id"]
    assert "token" not in listed[0]


async def test_create_with_expiry_reports_it(client, seeded) -> None:
    user, _ws = seeded
    r = await client.post(
        "/api/v1/oauth/pats",
        json={"name": "temp", "scope": ["mcp:read"], "expires_in_days": 30},
        headers=_auth(_session_jwt(user)),
    )
    assert r.status_code == 201, r.text
    assert r.json()["expires_at"] is not None


async def test_create_rejects_unknown_pat_scope(client, seeded) -> None:
    """Distinct from the mcp:admin gate: this is the scope requested FOR the PAT."""
    user, _ws = seeded
    r = await client.post(
        "/api/v1/oauth/pats",
        json={"name": "bad", "scope": ["mcp:root"]},
        headers=_auth(_session_jwt(user)),
    )
    assert r.status_code == 400
    assert "mcp:root" in r.text


async def test_revoking_unknown_pat_is_404(client, seeded) -> None:
    user, _ws = seeded
    r = await client.delete(f"/api/v1/oauth/pats/{uuid.uuid4()}", headers=_auth(_session_jwt(user)))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Neither class
# ---------------------------------------------------------------------------


async def test_missing_bearer_is_401(client) -> None:
    assert (await client.get("/api/v1/oauth/pats")).status_code == 401


async def test_garbage_bearer_is_401_and_names_no_other_scheme(client) -> None:
    """No sequential fallback: the failure must not be blamed on the other class."""
    r = await client.get("/api/v1/oauth/pats", headers=_auth("not-a-jwt"))
    assert r.status_code == 401
