"""``POST /safemode/runs/{run_id}/deny`` — 승인의 짝.

승인은 처음부터 런 단위 라우트가 있었는데(B12a) 거절은 없었다. 그래서 멀티아티팩트
런은 **한 항목씩만** 거절할 수 있었고, 형님이 직접 지목하지 않은 항목은 영원히 대기했다.
"""

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
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.delivery.db import SafeModeStatus

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


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
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_deny_run_settles_every_pending_item(client, db, workspace_id) -> None:
    run_id = uuid.uuid4()
    async with db() as s:
        q = SafeModeQueue(s)
        ids = [
            await q.enqueue(workspace_id=workspace_id, deliverable_id=uuid.uuid4(), run_id=run_id)
            for _ in range(3)
        ]
        await s.commit()

    r = await client.post(f"/api/v1/safemode/runs/{run_id}/deny", json={"reason": "방향이 다르다"})
    assert r.status_code == 200, r.text
    assert r.json()["denied_count"] == 3

    async with db() as s:
        rows = await SafeModeQueue(s).list_pending_for_run(workspace_id=workspace_id, run_id=run_id)
        assert rows == [], "거절 후에도 대기가 남았다"
        resolved = await SafeModeQueue(s).list_resolved(workspace_id=workspace_id)
        assert len(resolved) == 3
        assert all(r_.status is SafeModeStatus.DENIED for r_ in resolved)
        # 사유는 모든 행에 남는다 — 포착 경로(#760)가 행 단위로 읽는다.
        assert all(r_.deny_reason == "방향이 다르다" for r_ in resolved)
    assert len(ids) == 3


async def test_deny_run_with_nothing_pending_is_404(client, workspace_id) -> None:
    r = await client.post(f"/api/v1/safemode/runs/{uuid.uuid4()}/deny", json={"reason": ""})
    assert r.status_code == 404
