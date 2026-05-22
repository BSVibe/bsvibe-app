"""RBAC role-gating proof — require_role on an admin-gated route.

``DELETE /api/v1/products/{id}`` is gated by ``require_role("admin")``: the
caller's active ``Membership.role`` must rank at admin or owner. A viewer /
editor member is 403'd; an admin / owner passes. Authentication itself is
unchanged (a request with no membership is still 403 — no workspace).
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.deps import get_current_user, get_db_session
from backend.api.main import create_app
from backend.data import Base
from backend.identity.db import MembershipRow, UserRow
from backend.workspaces.db import ProductRow, WorkspaceRow

from .._support import fake_current_user

PG_URL = os.environ.get(
    "BSVIBE_DATABASE_URL", "postgresql+asyncpg://bsvibe:bsvibe@localhost:5442/bsvibe"
)

pytestmark = pytest.mark.asyncio


async def _can_reach_pg() -> bool:
    try:
        engine = create_async_engine(PG_URL, future=True, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def db():
    use_pg = os.environ.get("BSVIBE_DATABASE_URL") and await _can_reach_pg()
    url = PG_URL if use_pg else "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    if use_pg:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed(db, role: str) -> tuple[uuid.UUID, str]:
    """Seed a workspace + a member with ``role`` + a product. Returns ids."""
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    product_id = uuid.uuid4()
    supabase_user_id = f"sub-{role}"
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1", safe_mode=True))
        s.add(UserRow(id=user_id, supabase_user_id=supabase_user_id, email="m@x"))
        await s.flush()
        s.add(MembershipRow(id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, role=role))
        s.add(ProductRow(id=product_id, workspace_id=workspace_id, name="P", slug="p"))
        await s.commit()
    return product_id, supabase_user_id


def _client(app, db) -> httpx.AsyncClient:
    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_admin_or_owner_can_delete_product(db, role: str) -> None:
    product_id, sub = await _seed(db, role)
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user(sub)
    async with _client(app, db) as c:
        r = await c.delete(f"/api/v1/products/{product_id}")
    assert r.status_code == 204, r.text


@pytest.mark.parametrize("role", ["editor", "viewer"])
async def test_editor_or_viewer_cannot_delete_product(db, role: str) -> None:
    product_id, sub = await _seed(db, role)
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user(sub)
    async with _client(app, db) as c:
        r = await c.delete(f"/api/v1/products/{product_id}")
    assert r.status_code == 403, r.text
    assert "role" in r.json()["detail"].lower()


async def test_no_membership_is_403(db) -> None:
    # A principal authenticated but with no membership at all → 403 (no workspace).
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user("nobody")
    async with _client(app, db) as c:
        r = await c.delete(f"/api/v1/products/{uuid.uuid4()}")
    assert r.status_code == 403, r.text
