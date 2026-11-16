"""Drive slots are handed out fairly across workspaces (SKIP LOCKED kept)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.workers.agent_worker import AgentWorker

from .conftest import bootstrap_tenant, requires_real_pg

pytestmark = [pytest.mark.asyncio, requires_real_pg]

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def _seed(factory: async_sessionmaker[AsyncSession], ws: uuid.UUID, minute: int) -> uuid.UUID:
    run_id = uuid.uuid4()
    at = _T0 + timedelta(minutes=minute)
    async with factory() as session:
        async with workspace_session_scope(session, ws):
            session.add(
                ExecutionRun(
                    id=run_id,
                    workspace_id=ws,
                    status=RunStatus.OPEN,
                    payload={},
                    created_at=at,
                    updated_at=at,
                )
            )
            await session.flush()
        await session.commit()
    return run_id


async def test_other_workspace_gets_a_slot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    a = await bootstrap_tenant(
        session_factory, supabase_user_id=f"a-{uuid.uuid4()}", email="a@x.io"
    )
    b = await bootstrap_tenant(
        session_factory, supabase_user_id=f"b-{uuid.uuid4()}", email="b@x.io"
    )
    for i in range(5):
        await _seed(session_factory, a, i)
    b_run = await _seed(session_factory, b, 100)  # the most recent of all

    claimed = await AgentWorker(session_factory=session_factory)._claim_runs_for_drive(limit=2)

    assert len(claimed) == 2
    assert b_run in {rid for rid, _ in claimed}
