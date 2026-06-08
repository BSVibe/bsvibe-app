"""HTTP-surface tests for the external executor-worker registration router.

Exercises ``/api/v1/workers/*`` end-to-end over an httpx ``ASGITransport`` (the
``test_v1_*`` pattern). Auth axes under test:

* ``POST /register`` — ``Authorization: Bearer`` (Supabase JWT or MCP token).
* ``POST /heartbeat`` — ``X-Worker-Token`` header (NOT JWT).
* ``GET /workers`` / ``DELETE /workers/{id}`` — JWT, workspace-scoped.

Lift E5 (2026-06-06) — the legacy install-token mint endpoint and
``X-Install-Token`` register header are gone; the bearer path is the only
register surface.
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


def _override_bearer(app, workspace_id: uuid.UUID) -> None:
    """Make ``resolve_workspace_for_bearer`` return a fixed workspace id.

    The register endpoint imports the resolver as a module attribute, so we
    patch it on the imported workers module to redirect all bearers to a
    known workspace without exercising real JWT verification.
    """
    from backend.api.v1 import workers_register_auth

    async def _ok_bearer(bearer, session):  # noqa: ARG001
        return workers_register_auth.ResolvedRegisterPrincipal(
            workspace_id=workspace_id, auth_kind="supabase_jwt"
        )

    import backend.api.v1.workers as workers_mod

    workers_mod.resolve_workspace_for_bearer = _ok_bearer  # type: ignore[assignment]


def _restore_bearer() -> None:
    """Restore the real bearer resolver after a test patched it."""
    import backend.api.v1.workers as workers_mod
    from backend.api.v1.workers_register_auth import resolve_workspace_for_bearer as real

    workers_mod.resolve_workspace_for_bearer = real  # type: ignore[assignment]


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
async def ws_client(db, workspace_id):
    """JWT-authed client with workspace overridden (for list/revoke scoping)."""
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    app.dependency_overrides[get_current_user] = fake_current_user("ws-sub")
    app.dependency_overrides[get_workspace_id] = _ws
    async with _client(app, db) as c:
        yield c


# ── register (bearer-token authed, NOT JWT) ───────────────────────────────────


async def test_register_with_supabase_bearer_resolves_workspace(db, workspace_id) -> None:
    """Register with ``Authorization: Bearer`` (Supabase JWT) returns id + token."""
    await _seed_member(db, workspace_id, "owner", "bearer-register-sub")

    app = create_app()
    _override_bearer(app, workspace_id)
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
        # Lift E5 — the deprecation header is gone; the bearer path is the
        # ONLY register surface, so there is nothing to mark deprecated.
        assert "deprecation" not in {k.lower() for k in r.headers}
    finally:
        _restore_bearer()


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


async def test_register_without_authorization_is_401(db) -> None:
    """Lift E5 — no Authorization bearer → 401 (no install-token fallback)."""
    app = create_app()
    async with _client(app, db) as c:
        r = await c.post(
            "/api/v1/workers/register",
            json={"name": "x", "labels": [], "capabilities": []},
        )
    assert r.status_code == 401, r.text
    assert "Authorization bearer" in r.json()["detail"]


async def test_register_rejects_extra_fields(db, workspace_id) -> None:
    await _seed_member(db, workspace_id, "owner", "extra-sub")
    app = create_app()
    _override_bearer(app, workspace_id)
    try:
        async with _client(app, db) as c:
            r = await c.post(
                "/api/v1/workers/register",
                headers={"Authorization": "Bearer fake"},
                json={"name": "x", "labels": [], "capabilities": [], "bogus": 1},
            )
        assert r.status_code == 422, r.text
    finally:
        _restore_bearer()


async def test_register_bearer_ignores_body_workspace_id(db, workspace_id) -> None:
    """The body has no workspace_id field — extra='forbid' rejects sneaky inputs.

    Workspace MUST come from the verified bearer, never the body. This test
    asserts the schema-level guard: even sending a workspace_id field 422s.
    """
    await _seed_member(db, workspace_id, "owner", "owner-sub")

    app = create_app()
    _override_bearer(app, workspace_id)
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
        _restore_bearer()


# ── heartbeat (worker-token authed, NOT JWT) ──────────────────────────────────


async def test_heartbeat_with_worker_token_ok(db, workspace_id) -> None:
    await _seed_member(db, workspace_id, "owner", "hb-sub")
    app = create_app()
    _override_bearer(app, workspace_id)
    try:
        async with _client(app, db) as c:
            reg = await c.post(
                "/api/v1/workers/register",
                headers={"Authorization": "Bearer fake"},
                json={"name": "hb", "labels": [], "capabilities": []},
            )
            worker_token = reg.json()["token"]
    finally:
        _restore_bearer()

    app = create_app()  # no JWT — heartbeat is worker-token authed
    async with _client(app, db) as c:
        r = await c.post("/api/v1/workers/heartbeat", headers={"X-Worker-Token": worker_token})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


async def test_heartbeat_persists_in_flight_count(db, workspace_id) -> None:
    """Lift E16 — the heartbeat body's ``in_flight`` round-trips to ``last_in_flight``.

    The worker stamps its current ``len(in_flight)`` on every heartbeat;
    the backend persists it onto the row so ``find_available_worker`` can
    exclude saturated workers from selection. Without round-trip the
    capacity-aware dispatch can't fire and we silently fall back to the
    pre-E16 "always dispatch" behaviour.
    """
    from backend.executors.db import WorkerRow

    await _seed_member(db, workspace_id, "owner", "hb-if-sub")
    app = create_app()
    _override_bearer(app, workspace_id)
    try:
        async with _client(app, db) as c:
            reg = await c.post(
                "/api/v1/workers/register",
                headers={"Authorization": "Bearer fake"},
                json={"name": "hb-if", "labels": [], "capabilities": []},
            )
            worker_token = reg.json()["token"]
            worker_id = uuid.UUID(reg.json()["id"])
    finally:
        _restore_bearer()

    app = create_app()
    async with _client(app, db) as c:
        r = await c.post(
            "/api/v1/workers/heartbeat",
            headers={"X-Worker-Token": worker_token},
            json={"in_flight": 2},
        )
    assert r.status_code == 200, r.text
    async with db() as s:
        row = await s.get(WorkerRow, worker_id)
        assert row is not None
        assert row.last_in_flight == 2


async def test_heartbeat_without_body_defaults_in_flight_zero(db, workspace_id) -> None:
    """Lift E16 — a heartbeat with no body still works (defaults to 0).

    The endpoint pre-E16 took no body at all. After E16 the body shape is
    optional + defaults to ``in_flight=0`` so an older worker that still
    posts an empty body remains functional. This guards the back-compat
    rollout invariant.
    """
    from backend.executors.db import WorkerRow

    await _seed_member(db, workspace_id, "owner", "hb-nobody-sub")
    app = create_app()
    _override_bearer(app, workspace_id)
    try:
        async with _client(app, db) as c:
            reg = await c.post(
                "/api/v1/workers/register",
                headers={"Authorization": "Bearer fake"},
                json={"name": "hb-nobody", "labels": [], "capabilities": []},
            )
            worker_token = reg.json()["token"]
            worker_id = uuid.UUID(reg.json()["id"])
    finally:
        _restore_bearer()

    app = create_app()
    async with _client(app, db) as c:
        # No JSON body at all — must still succeed (older worker shape).
        r = await c.post(
            "/api/v1/workers/heartbeat",
            headers={"X-Worker-Token": worker_token},
        )
    assert r.status_code == 200, r.text
    async with db() as s:
        row = await s.get(WorkerRow, worker_id)
        assert row is not None
        assert row.last_in_flight == 0


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


async def test_list_workers_is_workspace_scoped(db, ws_client, workspace_id) -> None:
    app = create_app()
    _override_bearer(app, workspace_id)
    try:
        async with _client(app, db) as c:
            for name in ("w1", "w2"):
                await c.post(
                    "/api/v1/workers/register",
                    headers={"Authorization": "Bearer fake"},
                    json={"name": name, "labels": [], "capabilities": []},
                )
    finally:
        _restore_bearer()

    r = await ws_client.get("/api/v1/workers")
    assert r.status_code == 200, r.text
    names = {w["name"] for w in r.json()}
    assert names == {"w1", "w2"}


async def test_revoke_then_worker_auth_401(db, ws_client, workspace_id) -> None:
    app = create_app()
    _override_bearer(app, workspace_id)
    try:
        async with _client(app, db) as c:
            reg = await c.post(
                "/api/v1/workers/register",
                headers={"Authorization": "Bearer fake"},
                json={"name": "doomed", "labels": [], "capabilities": []},
            )
            worker_id = reg.json()["id"]
            worker_token = reg.json()["token"]
    finally:
        _restore_bearer()

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


# ── Lift E4 — list response carries timestamps ────────────────────────────────


async def test_register_response_carries_created_at_and_status(db, workspace_id) -> None:
    """List response after a bearer-register includes the new timestamps."""
    sub = "owner-sub"
    await _seed_member(db, workspace_id, "owner", sub)

    app = create_app()
    _override_bearer(app, workspace_id)

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
        _restore_bearer()


# ── Lift E5 regression: install-token surface is gone ─────────────────────────


async def test_install_token_endpoint_gone(admin_client) -> None:
    """The install-token mint endpoint was removed in Lift E5 — 404/405 now.

    FastAPI returns 405 (Method Not Allowed) when a sibling route's prefix
    catches the path but no method handler is registered, or 404 when the
    path is unknown entirely. Either is proof the endpoint is gone.
    """
    r = await admin_client.post("/api/v1/workers/install-token")
    assert r.status_code in (404, 405), r.text


# ── Lift E13 — fleet detail (capabilities/labels/heartbeat_fresh) ────────────


async def test_list_response_carries_e13_fleet_detail(db, workspace_id) -> None:
    """``GET /api/v1/workers`` carries the E13 fleet-detail fields per row:
    capabilities, labels, status, is_active, last_heartbeat, heartbeat_fresh,
    created_at.
    """
    from datetime import UTC, datetime, timedelta

    import backend.executors.db as executors_db  # noqa: PLC0415

    sub = "fleet-sub"
    await _seed_member(db, workspace_id, "owner", sub)

    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    app.dependency_overrides[get_current_user] = fake_current_user(sub)
    app.dependency_overrides[get_workspace_id] = _ws

    from backend.executors.dispatch import HEARTBEAT_FRESHNESS_S  # noqa: PLC0415

    now = datetime.now(UTC)
    async with db() as s:
        s.add(
            executors_db.WorkerRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                name="fresh",
                labels=["mac"],
                capabilities=["codex", "opencode"],
                status="online",
                is_active=True,
                last_heartbeat=now - timedelta(seconds=5),
                token_hash="h1",
            )
        )
        s.add(
            executors_db.WorkerRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                name="stale",
                labels=[],
                capabilities=["claude_code"],
                status="online",
                is_active=True,
                last_heartbeat=now - timedelta(seconds=HEARTBEAT_FRESHNESS_S + 60),
                token_hash="h2",
            )
        )
        await s.commit()

    async with _client(app, db) as c:
        r = await c.get("/api/v1/workers")
    assert r.status_code == 200, r.text
    rows = {row["name"]: row for row in r.json()}
    fresh = rows["fresh"]
    assert fresh["capabilities"] == ["codex", "opencode"]
    assert fresh["labels"] == ["mac"]
    assert fresh["status"] == "online"
    assert fresh["is_active"] is True
    assert fresh["last_heartbeat"]
    assert fresh["heartbeat_fresh"] is True
    assert fresh["created_at"]

    stale = rows["stale"]
    assert stale["status"] == "online"
    assert stale["heartbeat_fresh"] is False


async def test_register_with_only_x_install_token_is_401(db) -> None:
    """Lift E5 — the X-Install-Token header is no longer honoured."""
    app = create_app()
    async with _client(app, db) as c:
        r = await c.post(
            "/api/v1/workers/register",
            headers={"X-Install-Token": "anything"},
            json={"name": "x", "labels": [], "capabilities": []},
        )
    assert r.status_code == 401, r.text
    # Detail mentions Authorization bearer (the only path), not install-token.
    assert "Authorization bearer" in r.json()["detail"]
