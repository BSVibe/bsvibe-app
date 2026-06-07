"""``/api/v1/products/{slug_or_id}/bootstrap/{cancel,retry}`` — Lift E13.

The PWA needs a Cancel + Retry button on the bootstrap progress panel; the
MCP cancel/retry tools are mirrored 1:1 in REST so the same dispatch path
serves the founder UI and the MCP caller.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.identity.db import MembershipRow, UserRow  # noqa: F401
from backend.identity.workspaces_db import ProductRow, WorkspaceRow, WorkspacesBase

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client_with_ws(db):
    app = create_app()
    workspace_id = uuid.uuid4()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="test", region="us-1", safe_mode=True))
        user = UserRow(id=uuid.uuid4(), supabase_user_id="test-user", email="t@example.com")
        s.add(user)
        await s.flush()
        s.add(
            MembershipRow(id=uuid.uuid4(), user_id=user.id, workspace_id=workspace_id, role="owner")
        )
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, workspace_id


async def _seed_product(
    db,
    workspace_id,
    *,
    slug="p",
    bootstrap_status=None,
    repo_url="https://github.com/x/y",
    artifacts_count=None,
    error=None,
    progress=None,
):
    pid = uuid.uuid4()
    async with db() as s:
        s.add(
            ProductRow(
                id=pid,
                workspace_id=workspace_id,
                name="P",
                slug=slug,
                repo_url=repo_url,
                bootstrap_status=bootstrap_status,
                bootstrap_artifacts_count=artifacts_count,
                bootstrap_error=error,
                bootstrap_progress=progress,
            )
        )
        await s.commit()
    return pid


# ---------------------------------------------------------------------------
# POST /api/v1/products/{slug_or_id}/bootstrap/cancel
# ---------------------------------------------------------------------------
async def test_bootstrap_cancel_in_flight_by_slug(client_with_ws, db) -> None:
    c, workspace_id = client_with_ws
    pid = await _seed_product(db, workspace_id, slug="alpha", bootstrap_status="ingesting")

    r = await c.post("/api/v1/products/alpha/bootstrap/cancel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bootstrap_status"] == "failed"
    assert body["bootstrap_error"] == "cancelled by founder"
    assert body["id"] == str(pid)


async def test_bootstrap_cancel_in_flight_by_uuid(client_with_ws, db) -> None:
    c, workspace_id = client_with_ws
    pid = await _seed_product(db, workspace_id, slug="byuuid", bootstrap_status="cloning")

    r = await c.post(f"/api/v1/products/{pid}/bootstrap/cancel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bootstrap_status"] == "failed"


async def test_bootstrap_cancel_terminal_status_is_409(client_with_ws, db) -> None:
    c, workspace_id = client_with_ws
    await _seed_product(db, workspace_id, slug="done", bootstrap_status="complete")

    r = await c.post("/api/v1/products/done/bootstrap/cancel")
    assert r.status_code == 409, r.text
    assert "terminal" in r.json()["detail"]


async def test_bootstrap_cancel_unknown_product_is_404(client_with_ws) -> None:
    c, _ = client_with_ws
    r = await c.post("/api/v1/products/nonexistent/bootstrap/cancel")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/products/{slug_or_id}/bootstrap/retry
# ---------------------------------------------------------------------------
async def test_bootstrap_retry_resets_and_schedules(client_with_ws, db) -> None:
    from backend.api.v1.products.products_crud import get_bootstrap_scheduler  # noqa: PLC0415

    c, workspace_id = client_with_ws
    pid = await _seed_product(
        db,
        workspace_id,
        slug="retry-me",
        bootstrap_status="failed:ingest",
        artifacts_count=42,
        error="prior failure",
        progress={"chunks_done": 5},
    )

    captured: dict[str, object] = {}

    def _fake_scheduler(**kwargs):
        captured.update(kwargs)
        return None

    c._transport.app.dependency_overrides[get_bootstrap_scheduler] = (  # type: ignore[attr-defined]
        lambda: _fake_scheduler
    )

    r = await c.post("/api/v1/products/retry-me/bootstrap/retry")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bootstrap_status"] == "pending"
    assert body["bootstrap_artifacts_count"] is None
    assert body["bootstrap_error"] is None
    assert body["bootstrap_progress"] is None

    assert captured["product_id"] == pid
    assert captured["repo_url"] == "https://github.com/x/y"


async def test_bootstrap_retry_in_flight_is_409(client_with_ws, db) -> None:
    c, workspace_id = client_with_ws
    await _seed_product(db, workspace_id, slug="busy", bootstrap_status="ingesting")
    r = await c.post("/api/v1/products/busy/bootstrap/retry")
    assert r.status_code == 409, r.text
    assert "in flight" in r.json()["detail"]


async def test_bootstrap_retry_missing_repo_url_is_400(client_with_ws, db) -> None:
    c, workspace_id = client_with_ws
    await _seed_product(db, workspace_id, slug="norepo", bootstrap_status="failed", repo_url=None)
    r = await c.post("/api/v1/products/norepo/bootstrap/retry")
    assert r.status_code == 400, r.text
    assert "repo_url" in r.json()["detail"]


async def test_bootstrap_retry_unknown_product_is_404(client_with_ws) -> None:
    c, _ = client_with_ws
    r = await c.post("/api/v1/products/nonexistent/bootstrap/retry")
    assert r.status_code == 404
