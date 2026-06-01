"""Lift I-Repo-Workflow — SqlAlchemyRunRepository round-trip tests.

Uses ``tests._support.memory_session`` (in-memory SQLite) for speed; the
schema also runs against real PG via the suite's CI gates.
"""

from __future__ import annotations

import uuid

import pytest

from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.repositories import SqlAlchemyRunRepository
from tests._support import memory_session


@pytest.mark.asyncio
async def test_add_and_get_run_roundtrip() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyRunRepository(session)
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            status=RunStatus.OPEN,
            payload={"intent_text": "test"},
        )
        await repo.add(run)
        await session.flush()

        loaded = await repo.get(run.id)
        assert loaded is not None
        assert loaded.id == run.id
        assert loaded.status is RunStatus.OPEN


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyRunRepository(session)
        assert await repo.get(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_list_by_workspace_newest_first_respects_limit() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyRunRepository(session)
        workspace_id = uuid.uuid4()
        other_workspace = uuid.uuid4()

        # Insert 3 runs in the target workspace + 1 in a sibling workspace.
        from datetime import UTC, datetime, timedelta

        now = datetime.now(tz=UTC)
        ids = []
        for i in range(3):
            run_id = uuid.uuid4()
            ids.append(run_id)
            await repo.add(
                ExecutionRun(
                    id=run_id,
                    workspace_id=workspace_id,
                    status=RunStatus.OPEN,
                    payload={},
                    created_at=now - timedelta(minutes=2 - i),  # oldest first ordered i=0
                    updated_at=now,
                )
            )
        await repo.add(
            ExecutionRun(
                id=uuid.uuid4(),
                workspace_id=other_workspace,
                status=RunStatus.OPEN,
                payload={},
            )
        )
        await session.flush()

        rows = await repo.list_by_workspace(workspace_id)
        assert {r.workspace_id for r in rows} == {workspace_id}
        assert len(rows) == 3
        # Newest first
        assert rows[0].id == ids[2]
        assert rows[2].id == ids[0]

        # Limit applies
        limited = await repo.list_by_workspace(workspace_id, limit=2)
        assert len(limited) == 2


@pytest.mark.asyncio
async def test_find_by_request_id_returns_one_or_none() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyRunRepository(session)
        workspace_id = uuid.uuid4()
        request_id = uuid.uuid4()

        # No row yet → None
        assert await repo.find_by_request_id(request_id) is None

        # Insert + find
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            request_id=request_id,
            status=RunStatus.OPEN,
            payload={"request_id": str(request_id)},
        )
        await repo.add(run)
        await session.flush()

        found = await repo.find_by_request_id(request_id)
        assert found is not None
        assert found.id == run.id
