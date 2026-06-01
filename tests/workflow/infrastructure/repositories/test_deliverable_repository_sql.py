"""Lift I-Repo-Workflow-2 — SqlAlchemyDeliverableRepository round-trip tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.repositories import SqlAlchemyDeliverableRepository
from tests._support import memory_session


async def _seed_run(session, *, workspace_id: uuid.UUID) -> uuid.UUID:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        status=RunStatus.OPEN,
        payload={},
    )
    session.add(run)
    await session.flush()
    return run.id


@pytest.mark.asyncio
async def test_add_and_get_deliverable_roundtrip() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        run_id = await _seed_run(session, workspace_id=workspace_id)

        repo = SqlAlchemyDeliverableRepository(session)
        deliverable = Deliverable(
            id=uuid.uuid4(),
            run_id=run_id,
            workspace_id=workspace_id,
            deliverable_type=DeliverableType.CODE,
            payload={"artifact_refs": ["a.md"]},
        )
        await repo.add(deliverable)
        await session.flush()

        loaded = await repo.get(deliverable.id)
        assert loaded is not None
        assert loaded.id == deliverable.id
        assert loaded.workspace_id == workspace_id


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyDeliverableRepository(session)
        assert await repo.get(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_list_by_workspace_newest_first_and_workspace_scoped() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        sibling = uuid.uuid4()
        run_id = await _seed_run(session, workspace_id=workspace_id)
        sibling_run = await _seed_run(session, workspace_id=sibling)

        repo = SqlAlchemyDeliverableRepository(session)

        now = datetime.now(tz=UTC)
        ids = []
        for i in range(3):
            d_id = uuid.uuid4()
            ids.append(d_id)
            await repo.add(
                Deliverable(
                    id=d_id,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    deliverable_type=DeliverableType.CODE,
                    payload={},
                    created_at=now - timedelta(minutes=2 - i),
                )
            )
        # sibling-workspace deliverable should not appear
        await repo.add(
            Deliverable(
                id=uuid.uuid4(),
                run_id=sibling_run,
                workspace_id=sibling,
                deliverable_type=DeliverableType.CODE,
                payload={},
            )
        )
        await session.flush()

        rows = await repo.list_by_workspace(workspace_id)
        assert {r.workspace_id for r in rows} == {workspace_id}
        assert len(rows) == 3
        assert rows[0].id == ids[2]  # newest
        assert rows[2].id == ids[0]  # oldest


@pytest.mark.asyncio
async def test_list_by_workspace_run_filter_and_limit() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        run_a = await _seed_run(session, workspace_id=workspace_id)
        run_b = await _seed_run(session, workspace_id=workspace_id)

        repo = SqlAlchemyDeliverableRepository(session)
        for _ in range(2):
            await repo.add(
                Deliverable(
                    id=uuid.uuid4(),
                    run_id=run_a,
                    workspace_id=workspace_id,
                    deliverable_type=DeliverableType.CODE,
                    payload={},
                )
            )
        await repo.add(
            Deliverable(
                id=uuid.uuid4(),
                run_id=run_b,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={},
            )
        )
        await session.flush()

        only_a = await repo.list_by_workspace(workspace_id, run_id=run_a)
        assert len(only_a) == 2
        assert {r.run_id for r in only_a} == {run_a}

        limited = await repo.list_by_workspace(workspace_id, limit=2)
        assert len(limited) == 2


@pytest.mark.asyncio
async def test_list_by_run_oldest_first() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        run_id = await _seed_run(session, workspace_id=workspace_id)

        repo = SqlAlchemyDeliverableRepository(session)
        now = datetime.now(tz=UTC)
        ids = []
        for i in range(3):
            d_id = uuid.uuid4()
            ids.append(d_id)
            await repo.add(
                Deliverable(
                    id=d_id,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    deliverable_type=DeliverableType.CODE,
                    payload={},
                    created_at=now + timedelta(minutes=i),
                )
            )
        await session.flush()

        rows = await repo.list_by_run(run_id, workspace_id)
        assert [r.id for r in rows] == ids  # oldest first


@pytest.mark.asyncio
async def test_list_by_run_id_no_workspace_filter() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        run_id = await _seed_run(session, workspace_id=workspace_id)

        repo = SqlAlchemyDeliverableRepository(session)
        await repo.add(
            Deliverable(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"artifact_refs": ["a.md", "b.md"]},
            )
        )
        await session.flush()

        rows = await repo.list_by_run_id(run_id)
        assert len(rows) == 1
        assert rows[0].run_id == run_id


@pytest.mark.asyncio
async def test_find_first_by_run_or_none() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        run_id = await _seed_run(session, workspace_id=workspace_id)

        repo = SqlAlchemyDeliverableRepository(session)
        # nothing yet
        assert await repo.find_first_by_run(run_id) is None

        d_id = uuid.uuid4()
        await repo.add(
            Deliverable(
                id=d_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={},
            )
        )
        await session.flush()

        found = await repo.find_first_by_run(run_id)
        assert found is not None
        assert found.id == d_id
