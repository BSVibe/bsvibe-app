"""HTTP-surface tests for the external executor-worker registration router.

Exercises ``/api/v1/workers/*`` end-to-end over an httpx ``ASGITransport`` (the
``test_v1_*`` pattern). Auth axes under test:

* ``POST /install-token`` — JWT + ``require_role("admin")`` (privileged).
* ``POST /register`` — ``X-Install-Token`` header (NOT JWT).
* ``POST /heartbeat`` — ``X-Worker-Token`` header (NOT JWT).
* ``GET /workers`` / ``DELETE /workers/{id}`` — JWT, workspace-scoped.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

# Register the executor tables on Base.metadata for db_engine.create_all.
import backend.executors.db  # noqa: F401
from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import WorkspaceRow

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


async def _seed_member(db, workspace_id: uuid.UUID, role: str, sub: str) -> None:
    """Seed a workspace + a member with ``role`` so require_role resolves."""
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1", safe_mode=True))
        user_id = uuid.uuid4()
        s.add(UserRow(id=user_id, supabase_user_id=sub, email="m@x"))
        await s.flush()
        s.add(MembershipRow(id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, role=role))
        await s.commit()


def _client(app, db) -> httpx.AsyncClient:
    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest_asyncio.fixture
async def admin_client(db, workspace_id):
    """JWT-authed admin member of ``workspace_id`` (real require_role runs)."""
    sub = "admin-sub"
    await _seed_member(db, workspace_id, "admin", sub)
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user(sub)
    async with _client(app, db) as c:
        yield c


@pytest_asyncio.fixture
async def viewer_client(db, workspace_id):
    sub = "viewer-sub"
    await _seed_member(db, workspace_id, "viewer", sub)
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user(sub)
    async with _client(app, db) as c:
        yield c


@pytest_asyncio.fixture
async def ws_client(db, workspace_id):
    """JWT-authed client with workspace overridden (for list/revoke scoping)."""
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    app.dependency_overrides[get_current_user] = fake_current_user("ws-sub")
    app.dependency_overrides[get_workspace_id] = _ws
    async with _client(app, db) as c:
        yield c


# ── install-token (privileged) ────────────────────────────────────────────────


async def test_admin_can_mint_install_token(admin_client) -> None:
    r = await admin_client.post("/api/v1/workers/install-token")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"]
    assert isinstance(body["token"], str)


async def test_viewer_cannot_mint_install_token(viewer_client) -> None:
    r = await viewer_client.post("/api/v1/workers/install-token")
    assert r.status_code == 403, r.text


async def test_install_token_requires_jwt(db) -> None:
    """No bearer → 401 from the v1 router-level get_current_user gate."""
    app = create_app()
    async with _client(app, db) as c:
        r = await c.post("/api/v1/workers/install-token")
    assert r.status_code == 401, r.text


# ── register (install-token authed, NOT JWT) ──────────────────────────────────


async def test_register_with_install_token_returns_id_and_token(admin_client) -> None:
    mint = await admin_client.post("/api/v1/workers/install-token")
    install_token = mint.json()["token"]
    r = await admin_client.post(
        "/api/v1/workers/register",
        headers={"X-Install-Token": install_token},
        json={"name": "laptop-1", "labels": ["mac"], "capabilities": ["claude_code"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert uuid.UUID(body["id"])
    assert body["token"]


async def test_register_without_jwt_still_works(db, workspace_id, admin_client) -> None:
    """Register is install-token authed — a client with NO bearer can register."""
    install_token = (await admin_client.post("/api/v1/workers/install-token")).json()["token"]
    app = create_app()  # fresh app, NO get_current_user override → no JWT
    async with _client(app, db) as c:
        r = await c.post(
            "/api/v1/workers/register",
            headers={"X-Install-Token": install_token},
            json={"name": "headless", "labels": [], "capabilities": []},
        )
    assert r.status_code == 201, r.text


async def test_register_with_bad_install_token_is_401(db) -> None:
    app = create_app()
    async with _client(app, db) as c:
        r = await c.post(
            "/api/v1/workers/register",
            headers={"X-Install-Token": "bogus"},
            json={"name": "x", "labels": [], "capabilities": []},
        )
    assert r.status_code == 401, r.text


async def test_register_without_install_token_header_is_401(db) -> None:
    app = create_app()
    async with _client(app, db) as c:
        r = await c.post(
            "/api/v1/workers/register",
            json={"name": "x", "labels": [], "capabilities": []},
        )
    assert r.status_code == 401, r.text


async def test_register_rejects_extra_fields(admin_client) -> None:
    install_token = (await admin_client.post("/api/v1/workers/install-token")).json()["token"]
    r = await admin_client.post(
        "/api/v1/workers/register",
        headers={"X-Install-Token": install_token},
        json={"name": "x", "labels": [], "capabilities": [], "bogus": 1},
    )
    assert r.status_code == 422, r.text


# ── heartbeat (worker-token authed, NOT JWT) ──────────────────────────────────


async def test_heartbeat_with_worker_token_ok(db, admin_client) -> None:
    install_token = (await admin_client.post("/api/v1/workers/install-token")).json()["token"]
    reg = await admin_client.post(
        "/api/v1/workers/register",
        headers={"X-Install-Token": install_token},
        json={"name": "hb", "labels": [], "capabilities": []},
    )
    worker_token = reg.json()["token"]
    app = create_app()  # no JWT — heartbeat is worker-token authed
    async with _client(app, db) as c:
        r = await c.post("/api/v1/workers/heartbeat", headers={"X-Worker-Token": worker_token})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


async def test_heartbeat_with_bad_worker_token_is_401(db) -> None:
    app = create_app()
    async with _client(app, db) as c:
        r = await c.post("/api/v1/workers/heartbeat", headers={"X-Worker-Token": "nope"})
    assert r.status_code == 401, r.text


async def test_heartbeat_without_worker_token_is_401(db) -> None:
    app = create_app()
    async with _client(app, db) as c:
        r = await c.post("/api/v1/workers/heartbeat")
    assert r.status_code == 401, r.text


# ── list + revoke (JWT, workspace-scoped) ─────────────────────────────────────


async def test_list_workers_is_workspace_scoped(ws_client, admin_client) -> None:
    install_token = (await admin_client.post("/api/v1/workers/install-token")).json()["token"]
    for name in ("w1", "w2"):
        await admin_client.post(
            "/api/v1/workers/register",
            headers={"X-Install-Token": install_token},
            json={"name": name, "labels": [], "capabilities": []},
        )
    r = await ws_client.get("/api/v1/workers")
    assert r.status_code == 200, r.text
    names = {w["name"] for w in r.json()}
    assert names == {"w1", "w2"}


async def test_revoke_then_worker_auth_401(db, ws_client, admin_client) -> None:
    install_token = (await admin_client.post("/api/v1/workers/install-token")).json()["token"]
    reg = await admin_client.post(
        "/api/v1/workers/register",
        headers={"X-Install-Token": install_token},
        json={"name": "doomed", "labels": [], "capabilities": []},
    )
    worker_id = reg.json()["id"]
    worker_token = reg.json()["token"]

    r = await ws_client.delete(f"/api/v1/workers/{worker_id}")
    assert r.status_code == 204, r.text

    # The revoked worker no longer authenticates.
    app = create_app()
    async with _client(app, db) as c:
        hb = await c.post("/api/v1/workers/heartbeat", headers={"X-Worker-Token": worker_token})
    assert hb.status_code == 401, hb.text


async def test_revoke_unknown_worker_is_404(ws_client) -> None:
    r = await ws_client.delete(f"/api/v1/workers/{uuid.uuid4()}")
    assert r.status_code == 404, r.text


# ── Lift E4 — bearer-token register path ──────────────────────────────────────


async def test_install_token_endpoint_advertises_deprecation_header(admin_client) -> None:
    r = await admin_client.post("/api/v1/workers/install-token")
    assert r.status_code == 200
    assert r.headers.get("deprecation") == "true"
    assert "successor-version" in (r.headers.get("link") or "")


async def test_register_with_supabase_bearer_resolves_workspace(db, workspace_id) -> None:
    """Lift E4 — register with ``Authorization: Bearer`` (Supabase JWT)."""
    sub = "bearer-register-sub"
    await _seed_member(db, workspace_id, "owner", sub)

    from backend.api.v1 import workers_register_auth

    async def _ok_bearer(bearer, session):  # noqa: ARG001
        return workers_register_auth.ResolvedRegisterPrincipal(
            workspace_id=workspace_id, auth_kind="supabase_jwt"
        )

    app = create_app()
    # The route hits the standalone resolver — patch that, not get_current_user.
    import backend.api.v1.workers as workers_mod

    workers_mod.resolve_workspace_for_bearer = _ok_bearer  # type: ignore[assignment]
    try:
        async with _client(app, db) as c:
            r = await c.post(
                "/api/v1/workers/register",
                headers={"Authorization": "Bearer fake-but-resolved"},
                json={"name": "mac-mini", "labels": [], "capabilities": ["claude_code"]},
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert uuid.UUID(body["id"])
        assert body["token"]
        assert "deprecation" not in {k.lower() for k in r.headers}
    finally:
        # restore
        from backend.api.v1.workers_register_auth import (
            resolve_workspace_for_bearer as real_resolver,
        )

        workers_mod.resolve_workspace_for_bearer = real_resolver  # type: ignore[assignment]


async def test_register_with_invalid_bearer_is_401(db) -> None:
    """A bearer that fails BOTH MCP and Supabase JWT verification → 401."""
    app = create_app()
    async with _client(app, db) as c:
        r = await c.post(
            "/api/v1/workers/register",
            headers={"Authorization": "Bearer obviously-not-a-jwt"},
            json={"name": "x", "labels": [], "capabilities": []},
        )
    assert r.status_code == 401, r.text
    assert "invalid bearer token" in r.json()["detail"]


async def test_register_without_any_auth_is_401(db) -> None:
    app = create_app()
    async with _client(app, db) as c:
        r = await c.post(
            "/api/v1/workers/register",
            json={"name": "x", "labels": [], "capabilities": []},
        )
    assert r.status_code == 401, r.text
    assert "Authorization bearer or X-Install-Token" in r.json()["detail"]


async def test_register_bearer_ignores_body_workspace_id(db, workspace_id) -> None:
    """The body has no workspace_id field — extra='forbid' rejects sneaky inputs.

    Workspace MUST come from the verified bearer, never the body. This test
    asserts the schema-level guard: even sending a workspace_id field 422s.
    """
    sub = "owner-sub"
    await _seed_member(db, workspace_id, "owner", sub)

    from backend.api.v1 import workers_register_auth

    async def _ok_bearer(bearer, session):  # noqa: ARG001
        return workers_register_auth.ResolvedRegisterPrincipal(
            workspace_id=workspace_id, auth_kind="supabase_jwt"
        )

    app = create_app()
    import backend.api.v1.workers as workers_mod

    workers_mod.resolve_workspace_for_bearer = _ok_bearer  # type: ignore[assignment]
    try:
        async with _client(app, db) as c:
            r = await c.post(
                "/api/v1/workers/register",
                headers={"Authorization": "Bearer fake"},
                json={
                    "name": "x",
                    "labels": [],
                    "capabilities": [],
                    "workspace_id": str(uuid.uuid4()),  # forbidden extra
                },
            )
        assert r.status_code == 422, r.text
    finally:
        from backend.api.v1.workers_register_auth import (
            resolve_workspace_for_bearer as real_resolver,
        )

        workers_mod.resolve_workspace_for_bearer = real_resolver  # type: ignore[assignment]


async def test_register_legacy_install_token_still_works(admin_client) -> None:
    """Backward-compat — the X-Install-Token path is preserved through Lift E5."""
    install_token = (await admin_client.post("/api/v1/workers/install-token")).json()["token"]
    r = await admin_client.post(
        "/api/v1/workers/register",
        headers={"X-Install-Token": install_token},
        json={"name": "legacy", "labels": [], "capabilities": []},
    )
    assert r.status_code == 201, r.text
    # The deprecated install_token register response carries the Deprecation
    # signal so callers know to migrate.
    assert r.headers.get("deprecation") == "true"


async def test_register_response_carries_created_at_and_status(db, workspace_id) -> None:
    """List response after a bearer-register includes the new timestamps."""
    sub = "owner-sub"
    await _seed_member(db, workspace_id, "owner", sub)

    from backend.api.v1 import workers_register_auth

    async def _ok_bearer(bearer, session):  # noqa: ARG001
        return workers_register_auth.ResolvedRegisterPrincipal(
            workspace_id=workspace_id, auth_kind="supabase_jwt"
        )

    app = create_app()
    import backend.api.v1.workers as workers_mod

    workers_mod.resolve_workspace_for_bearer = _ok_bearer  # type: ignore[assignment]

    def _ws() -> uuid.UUID:
        return workspace_id

    app.dependency_overrides[get_current_user] = fake_current_user(sub)
    app.dependency_overrides[get_workspace_id] = _ws
    try:
        async with _client(app, db) as c:
            await c.post(
                "/api/v1/workers/register",
                headers={"Authorization": "Bearer fake"},
                json={"name": "stamped", "labels": [], "capabilities": ["claude_code"]},
            )
            lr = await c.get("/api/v1/workers")
        assert lr.status_code == 200, lr.text
        rows = lr.json()
        assert len(rows) == 1
        assert rows[0]["name"] == "stamped"
        assert rows[0]["created_at"]  # ISO 8601 string
        # No heartbeat yet — explicitly null.
        assert rows[0]["last_heartbeat"] is None
    finally:
        from backend.api.v1.workers_register_auth import (
            resolve_workspace_for_bearer as real_resolver,
        )

        workers_mod.resolve_workspace_for_bearer = real_resolver  # type: ignore[assignment]
