"""``GET /safemode/queue`` and ``/queue/by-run`` carry the change footprint of each held item."""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.identity.db import UserRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine, fake_current_user, publishing_workspace
from ..mcp._rls import scoped_session

pytestmark = pytest.mark.asyncio

BIG_DIR = ".pnpm-store"


@pytest_asyncio.fixture
async def db():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(db, workspace_id):
    app = create_app()
    user = UserRow(id=uuid.uuid4(), supabase_user_id=f"sub-{uuid.uuid4().hex}")

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_current_user_row] = lambda: user
    app.dependency_overrides[get_workspace_id] = publishing_workspace(workspace_id)
    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _hold(db, workspace_id: uuid.UUID, payload: dict) -> uuid.UUID:
    async with scoped_session(db, workspace_id) as s:
        if await s.get(WorkspaceRow, workspace_id) is None:
            s.add(WorkspaceRow(id=workspace_id, name="ws"))
            await s.flush()
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=None,
            status=RunStatus.RUNNING,
            payload={},
        )
        s.add(run)
        await s.flush()
        run_id = run.id
        deliverable = Deliverable(
            id=uuid.uuid4(),
            run_id=run_id,
            workspace_id=workspace_id,
            deliverable_type=DeliverableType.CODE,
            payload=payload,
        )
        s.add(deliverable)
        await s.flush()
        await SafeModeQueue(s).enqueue(
            workspace_id=workspace_id, deliverable_id=deliverable.id, run_id=run_id
        )
        await s.commit()
        return deliverable.id


async def test_queue_reports_worktree_footprint_for_a_large_change(client, db, workspace_id):
    deliverable_id = await _hold(
        db,
        workspace_id,
        {
            "artifact_refs": ["src/a.py"],
            "worktree_change": {
                "changed_files": 18752,
                "top_dirs": [{"dir": BIG_DIR, "count": 18745}, {"dir": "src", "count": 7}],
            },
        },
    )

    r = await client.get("/api/v1/safemode/queue")
    assert r.status_code == 200, r.text
    (item,) = [i for i in r.json() if i["deliverable_id"] == str(deliverable_id)]
    assert item["changed_files"] == 18752
    assert item["large_change"] is True
    assert item["top_dirs"][0] == {"dir": BIG_DIR, "count": 18745}
    assert BIG_DIR in item["large_change_warning"]

    r = await client.get("/api/v1/safemode/queue/by-run")
    assert r.status_code == 200, r.text
    (item,) = [i for g in r.json() for i in g["items"]]
    assert item["changed_files"] == 18752
    assert item["large_change"] is True


async def test_queue_small_change_has_no_warning(client, db, workspace_id):
    await _hold(db, workspace_id, {"artifact_refs": ["src/a.py", "src/b.py", "README.md"]})

    r = await client.get("/api/v1/safemode/queue")

    (item,) = r.json()
    assert item["changed_files"] == 3
    assert item["large_change"] is False
    assert not item["large_change_warning"]


async def test_queue_item_without_any_count_omits_changed_files(client, db, workspace_id):
    await _hold(db, workspace_id, {})

    r = await client.get("/api/v1/safemode/queue")

    (item,) = r.json()
    assert item["changed_files"] is None
    assert item["large_change"] is False
    assert item["top_dirs"] == []
