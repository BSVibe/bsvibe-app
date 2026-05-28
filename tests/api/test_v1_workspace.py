"""``/api/v1/workspace`` — GET + PATCH the caller's workspace name.

Tests the everyday workspace metadata routes registered alongside the
compliance ones under the singular ``/workspace`` prefix.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.identity.db import MembershipRow, UserRow
from backend.workspaces.db import WorkspaceRow, WorkspacesBase

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
    user_id = uuid.uuid4()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    async with db() as s:
        s.add(
            WorkspaceRow(
                id=workspace_id,
                name="Acme",
                region="us-1",
                safe_mode=True,
                legal_basis="contract",
            )
        )
        s.add(UserRow(id=user_id, supabase_user_id="test-user", email="t@example.com"))
        await s.flush()
        s.add(
            MembershipRow(id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, role="owner")
        )
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, workspace_id, db


async def test_get_workspace_returns_id_and_name(client_with_ws) -> None:
    c, workspace_id, _ = client_with_ws
    r = await c.get("/api/v1/workspace")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"id": str(workspace_id), "name": "Acme"}


async def test_patch_workspace_renames_and_persists(client_with_ws) -> None:
    c, workspace_id, db = client_with_ws
    r = await c.patch("/api/v1/workspace", json={"name": "Acme Inc."})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": str(workspace_id), "name": "Acme Inc."}

    # The row in the database actually changed.
    async with db() as s:
        row = (
            await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        ).scalar_one()
        assert row.name == "Acme Inc."


async def test_patch_workspace_trims_whitespace(client_with_ws) -> None:
    c, workspace_id, _ = client_with_ws
    r = await c.patch("/api/v1/workspace", json={"name": "   Renamed Co.   "})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed Co."


async def test_patch_workspace_rejects_empty_name(client_with_ws) -> None:
    c, _workspace_id, _ = client_with_ws
    r = await c.patch("/api/v1/workspace", json={"name": ""})
    assert r.status_code == 422  # Field(min_length=1)


async def test_patch_workspace_rejects_extra_fields(client_with_ws) -> None:
    """``extra="forbid"`` — unknown keys (e.g. region, legal_basis) are
    rejected so writes can't quietly mutate fields the route doesn't own."""
    c, _workspace_id, _ = client_with_ws
    r = await c.patch("/api/v1/workspace", json={"name": "Renamed", "region": "moon-1"})
    assert r.status_code == 422


async def test_get_workspace_unauthenticated_rejected(db) -> None:
    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/v1/workspace")
        assert r.status_code == 401
