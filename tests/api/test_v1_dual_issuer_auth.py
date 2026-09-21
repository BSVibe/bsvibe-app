"""The v1 gate must accept BOTH credential classes — with real tokens (#1017).

`bsvibe login` does not store a Supabase session JWT. It stores an **ES256
access token issued by our own embedded OAuth server** (`iss` = the BSVibe
issuer, `sub` = ``UserRow.id``, plus a ``wsp`` claim). The v1 router's gate
verified only the Supabase class, so every ordinary CLI command — `bsvibe
products list`, `runs list`, `deliverables show` — 401'd in production while
the whole suite stayed green: every existing test signs its own session JWT.

That is why `/api/v1/oauth/pats` is mounted OUTSIDE the v1 gate (see
``backend.api.main``). One router escaped; the rest of v1 did not.

Nothing here overrides authentication. Real tokens go over the wire and the
production resolver verifies them; only the DB session is pointed at the test
database.
"""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
import backend.workflow.infrastructure.db  # noqa: F401
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
SESSION_SECRET = "test-session-secret-that-is-long-enough"
CLI_SCOPES = ["mcp:read", "mcp:write", "mcp:admin"]


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator[Any]:
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    monkeypatch.setenv("BSVIBE_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("USER_JWT_SECRET", SESSION_SECRET)
    monkeypatch.setenv("USER_JWT_ALGORITHM", "HS256")
    monkeypatch.delenv("USER_JWT_JWKS_URL", raising=False)
    get_settings.cache_clear()
    get_authz_settings.cache_clear()
    reset_signing_key_for_tests()
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()
    get_authz_settings.cache_clear()
    reset_signing_key_for_tests()


async def _seed_user_with_workspace(db, *, name: str) -> tuple[UserRow, uuid.UUID]:
    async with db() as s:
        ws = WorkspaceRow(name=name)
        s.add(ws)
        user = UserRow(supabase_user_id=f"sb-{uuid.uuid4()}", email=f"{name}@example.com")
        s.add(user)
        await s.flush()
        s.add(MembershipRow(user_id=user.id, workspace_id=ws.id, role="owner"))
        await s.commit()
        return user, ws.id


@pytest_asyncio.fixture
async def seeded(db) -> tuple[UserRow, uuid.UUID]:
    return await _seed_user_with_workspace(db, name="t-ws")


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


async def _cli_token(db, user: UserRow, workspace_id: uuid.UUID, scope: list[str]) -> str:
    """The credential ``bsvibe login`` writes to ~/.config/bsvibe/credentials.json."""
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
# The regression the issue asks for: the CLI's OWN token reaches v1
# ---------------------------------------------------------------------------


async def test_cli_access_token_reaches_a_v1_route(client, db, seeded) -> None:
    """`bsvibe products list` — the exact call that 401'd in production."""
    user, ws = seeded
    token = await _cli_token(db, user, ws, CLI_SCOPES)

    r = await client.get("/api/v1/products", headers=_auth(token))

    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_session_jwt_still_reaches_a_v1_route(client, seeded) -> None:
    """The PWA's credential class must be untouched by widening the gate."""
    user, _ws = seeded

    r = await client.get("/api/v1/products", headers=_auth(_session_jwt(user)))

    assert r.status_code == 200, r.text


async def test_no_bearer_is_still_401(client) -> None:
    r = await client.get("/api/v1/products")
    assert r.status_code == 401


async def test_garbage_bearer_is_401_not_500(client) -> None:
    r = await client.get("/api/v1/products", headers=_auth("not-a-jwt"))
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Widening an auth surface is where escalation hides
# ---------------------------------------------------------------------------


async def test_run_scoped_task_token_cannot_reach_v1(client, db, seeded) -> None:
    """A dispatched executor's 90-minute credential is for the work tools only.

    It reaches ONE run's worktree over /mcp. If it also opened the whole v1
    API, a leaked task token would be a workspace-wide foothold.
    """
    user, ws = seeded
    async with db() as s:
        token = await issue_run_task_token(
            s, run_id=uuid.uuid4(), workspace_id=ws, user_id=user.id, issuer=ISSUER
        )
        await s.commit()

    r = await client.get("/api/v1/products", headers=_auth(token))

    assert r.status_code == 403, r.text
    assert "run-scoped" in r.text


async def test_read_only_token_cannot_mutate(client, db, seeded) -> None:
    """mcp:read alone reads. The 404 control below proves the 403 is the SCOPE."""
    user, ws = seeded
    token = await _cli_token(db, user, ws, ["mcp:read"])

    r = await client.patch(
        f"/api/v1/products/{uuid.uuid4()}", json={"name": "x"}, headers=_auth(token)
    )

    assert r.status_code == 403, r.text
    assert "mcp:write" in r.text


async def test_write_scoped_token_passes_the_scope_gate(client, db, seeded) -> None:
    """Positive control for the test above: with mcp:write the request REACHES
    the handler — 404 for an unknown id, not 403."""
    user, ws = seeded
    token = await _cli_token(db, user, ws, CLI_SCOPES)

    r = await client.patch(
        f"/api/v1/products/{uuid.uuid4()}", json={"name": "x"}, headers=_auth(token)
    )

    assert r.status_code == 404, r.text


async def test_revoked_access_token_is_rejected(client, db, seeded) -> None:
    user, ws = seeded
    token = await _cli_token(db, user, ws, CLI_SCOPES)
    claims = jwt.decode(token, options={"verify_signature": False})
    async with db() as s:
        await s.execute(
            update(OAuthAccessTokenRow)
            .where(OAuthAccessTokenRow.id == uuid.UUID(claims["jti"]))
            .values(revoked_at=datetime.now(UTC))
        )
        await s.commit()

    r = await client.get("/api/v1/products", headers=_auth(token))

    assert r.status_code == 401, r.text


async def test_row_expiry_is_enforced_even_when_the_jwt_still_looks_fresh(
    client, db, seeded
) -> None:
    """The ROW is the authority on lifetime — shortening it must take effect
    without reissuing the JWT (``backend.mcp.auth`` states the rule; this pins
    it at the v1 gate too)."""
    user, ws = seeded
    token = await _cli_token(db, user, ws, CLI_SCOPES)
    claims = jwt.decode(token, options={"verify_signature": False})
    async with db() as s:
        await s.execute(
            update(OAuthAccessTokenRow)
            .where(OAuthAccessTokenRow.id == uuid.UUID(claims["jti"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()

    r = await client.get("/api/v1/products", headers=_auth(token))

    assert r.status_code == 401, r.text


async def test_token_scopes_to_its_wsp_claim_not_the_users_first_membership(client, db) -> None:
    """Isolation: a token NAMES its workspace. Resolving the workspace from the
    caller's membership instead would silently put the CLI in a different
    tenant than the credential it presented — and the founder really does hold
    memberships in more than one workspace."""
    user, ws_a = await _seed_user_with_workspace(db, name="ws-a")
    async with db() as s:
        ws_b = WorkspaceRow(name="ws-b")
        s.add(ws_b)
        await s.flush()
        s.add(MembershipRow(user_id=user.id, workspace_id=ws_b.id, role="owner"))
        await s.commit()
        ws_b_id = ws_b.id

    token_b = await _cli_token(db, user, ws_b_id, CLI_SCOPES)
    r = await client.get("/api/v1/workspace", headers=_auth(token_b))

    assert r.status_code == 200, r.text
    assert r.json()["id"] == str(ws_b_id)
    assert str(ws_a) != str(ws_b_id)


async def test_token_for_a_workspace_the_user_left_is_rejected(client, db, seeded) -> None:
    """Membership is checked at the gate: losing access must not wait for the
    token's own expiry."""
    user, ws = seeded
    token = await _cli_token(db, user, ws, CLI_SCOPES)
    async with db() as s:
        await s.execute(
            update(MembershipRow)
            .where(MembershipRow.user_id == user.id, MembershipRow.workspace_id == ws)
            .values(left_at=datetime.now(UTC))
        )
        await s.commit()

    r = await client.get("/api/v1/products", headers=_auth(token))

    assert r.status_code == 403, r.text
