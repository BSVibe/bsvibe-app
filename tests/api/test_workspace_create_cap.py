"""POST /api/v1/workspaces — a user may own only ``max_owned_workspaces`` live workspaces.

Concurrency, verify slots and the plan are all per-workspace, so unlimited creation
multiplies every limit. The count is taken under a lock on the user's row.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update

import backend.identity.db  # noqa: F401 — register mappers
import backend.identity.workspaces_db  # noqa: F401
from backend.api.deps import get_current_user, get_db_session
from backend.api.main import create_app
from backend.config import get_settings
from backend.identity.db import UserRow
from backend.identity.workspaces_db import WorkspaceRow

from .._support import db_engine, fake_current_user
from .conftest import commit_per_workspace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def env() -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with db_engine() as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        app = create_app()

        async def _session():
            async with sf() as s:
                yield s

        async with sf() as s:
            s.add(UserRow(id=uuid.uuid4(), supabase_user_id="cap-user", email="c@x"))
            await commit_per_workspace(s)

        app.dependency_overrides[get_db_session] = _session
        app.dependency_overrides[get_current_user] = fake_current_user("cap-user")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, sf


async def test_creates_up_to_the_cap_then_refuses(env) -> None:
    c, _ = env
    limit = get_settings().max_owned_workspaces
    assert limit == 3
    for i in range(limit):
        r = await c.post("/api/v1/workspaces", json={"name": f"ws{i}"})
        assert r.status_code == 201, r.text
    r = await c.post("/api/v1/workspaces", json={"name": "one too many"})
    assert r.status_code == 409, r.text
    assert "limit" in r.json()["detail"]
    listed = await c.get("/api/v1/workspaces")
    assert len(listed.json()) == limit


async def test_deleting_frees_a_slot(env) -> None:
    c, _ = env
    ids = []
    for i in range(3):
        r = await c.post("/api/v1/workspaces", json={"name": f"ws{i}"})
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    assert (await c.post("/api/v1/workspaces", json={"name": "x"})).status_code == 409
    assert (await c.delete(f"/api/v1/workspaces/{ids[0]}")).status_code == 204
    r = await c.post("/api/v1/workspaces", json={"name": "again"})
    assert r.status_code == 201, r.text


async def test_operator_owner_is_exempt(env) -> None:
    c, sf = env
    first = await c.post("/api/v1/workspaces", json={"name": "ws0"})
    assert first.status_code == 201, first.text
    ws_id = uuid.UUID(first.json()["id"])
    async with sf() as s:
        from backend.data.rls import workspace_session_scope

        async with workspace_session_scope(s, ws_id):
            await s.execute(
                update(WorkspaceRow).where(WorkspaceRow.id == ws_id).values(plan="operator")
            )
        await s.commit()
    for i in range(1, 6):
        r = await c.post("/api/v1/workspaces", json={"name": f"ws{i}"})
        assert r.status_code == 201, r.text
