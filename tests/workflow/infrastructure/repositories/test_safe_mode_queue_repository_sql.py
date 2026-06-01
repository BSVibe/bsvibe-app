"""Lift I-Repo-Workflow-2 — SqlAlchemySafeModeQueueRepository round-trip tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.workflow.infrastructure.delivery.db import (
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from backend.workflow.infrastructure.repositories import (
    SqlAlchemySafeModeQueueRepository,
)
from tests._support import memory_session


def _make_item(
    *,
    workspace_id: uuid.UUID,
    status: SafeModeStatus = SafeModeStatus.PENDING,
    run_id: uuid.UUID | None = None,
    expires_in_days: int = 90,
    decided_at: datetime | None = None,
    created_at: datetime | None = None,
) -> SafeModeQueueItemRow:
    now = datetime.now(tz=UTC)
    return SafeModeQueueItemRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        deliverable_id=uuid.uuid4(),
        run_id=run_id,
        status=status,
        expires_at=now + timedelta(days=expires_in_days),
        extension_count=0,
        created_at=created_at or now,
        decided_at=decided_at,
    )


@pytest.mark.asyncio
async def test_add_and_get_roundtrip() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        repo = SqlAlchemySafeModeQueueRepository(session)
        item = _make_item(workspace_id=workspace_id)
        await repo.add(item)
        await session.flush()

        loaded = await repo.get(item.id)
        assert loaded is not None
        assert loaded.id == item.id
        assert loaded.status is SafeModeStatus.PENDING


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    async with memory_session() as session:
        repo = SqlAlchemySafeModeQueueRepository(session)
        assert await repo.get(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_list_pending_by_workspace_newest_first_and_scoped() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        sibling = uuid.uuid4()
        repo = SqlAlchemySafeModeQueueRepository(session)

        now = datetime.now(tz=UTC)
        ids = []
        for i in range(3):
            item = _make_item(
                workspace_id=workspace_id,
                created_at=now - timedelta(minutes=2 - i),
            )
            ids.append(item.id)
            await repo.add(item)
        # Other workspace
        await repo.add(_make_item(workspace_id=sibling))
        # Decided rows should not appear in pending list
        await repo.add(_make_item(workspace_id=workspace_id, status=SafeModeStatus.APPROVED))
        await session.flush()

        rows = await repo.list_pending_by_workspace(workspace_id)
        assert {r.workspace_id for r in rows} == {workspace_id}
        assert all(r.status is SafeModeStatus.PENDING for r in rows)
        assert len(rows) == 3
        assert rows[0].id == ids[2]  # newest first
        assert rows[2].id == ids[0]


@pytest.mark.asyncio
async def test_list_pending_for_run_oldest_first() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        run_id = uuid.uuid4()
        repo = SqlAlchemySafeModeQueueRepository(session)

        now = datetime.now(tz=UTC)
        ids = []
        for i in range(3):
            item = _make_item(
                workspace_id=workspace_id,
                run_id=run_id,
                created_at=now + timedelta(minutes=i),
            )
            ids.append(item.id)
            await repo.add(item)
        await repo.add(_make_item(workspace_id=workspace_id))  # no run_id
        await session.flush()

        rows = await repo.list_pending_for_run(workspace_id=workspace_id, run_id=run_id)
        assert [r.id for r in rows] == ids  # oldest first


@pytest.mark.asyncio
async def test_list_resolved_by_workspace_decided_first() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        repo = SqlAlchemySafeModeQueueRepository(session)

        now = datetime.now(tz=UTC)
        approved = _make_item(
            workspace_id=workspace_id,
            status=SafeModeStatus.APPROVED,
            decided_at=now - timedelta(minutes=5),
        )
        denied = _make_item(
            workspace_id=workspace_id,
            status=SafeModeStatus.DENIED,
            decided_at=now - timedelta(minutes=1),
        )
        # Pending should not appear
        pending = _make_item(workspace_id=workspace_id)
        for it in (approved, denied, pending):
            await repo.add(it)
        await session.flush()

        rows = await repo.list_resolved_by_workspace(workspace_id)
        assert [r.id for r in rows] == [denied.id, approved.id]


@pytest.mark.asyncio
async def test_list_due_expired_cross_workspace() -> None:
    async with memory_session() as session:
        repo = SqlAlchemySafeModeQueueRepository(session)
        now = datetime.now(tz=UTC)

        past = SafeModeQueueItemRow(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            deliverable_id=uuid.uuid4(),
            status=SafeModeStatus.PENDING,
            expires_at=now - timedelta(days=1),
            extension_count=0,
            created_at=now - timedelta(days=2),
        )
        future = SafeModeQueueItemRow(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            deliverable_id=uuid.uuid4(),
            status=SafeModeStatus.PENDING,
            expires_at=now + timedelta(days=1),
            extension_count=0,
            created_at=now,
        )
        await repo.add(past)
        await repo.add(future)
        await session.flush()

        rows = await repo.list_due_expired(now=now)
        assert {r.id for r in rows} == {past.id}


@pytest.mark.asyncio
async def test_mark_expired_bulk_workspace_scoped() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        other = uuid.uuid4()
        repo = SqlAlchemySafeModeQueueRepository(session)
        now = datetime.now(tz=UTC)

        # 2 expired in target workspace
        for _ in range(2):
            await repo.add(
                SafeModeQueueItemRow(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    deliverable_id=uuid.uuid4(),
                    status=SafeModeStatus.PENDING,
                    expires_at=now - timedelta(days=1),
                    extension_count=0,
                    created_at=now - timedelta(days=2),
                )
            )
        # 1 expired in other workspace — must not be touched
        other_item = SafeModeQueueItemRow(
            id=uuid.uuid4(),
            workspace_id=other,
            deliverable_id=uuid.uuid4(),
            status=SafeModeStatus.PENDING,
            expires_at=now - timedelta(days=1),
            extension_count=0,
            created_at=now - timedelta(days=2),
        )
        await repo.add(other_item)
        await session.flush()

        count = await repo.mark_expired_bulk(workspace_id=workspace_id, now=now)
        assert count == 2

        # other workspace's row still PENDING
        loaded = await repo.get(other_item.id)
        assert loaded is not None
        assert loaded.status is SafeModeStatus.PENDING
