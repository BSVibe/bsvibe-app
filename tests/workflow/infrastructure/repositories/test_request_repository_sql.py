"""Lift I-Repo-Workflow-3 — SqlAlchemyRequestRepository round-trip tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.repositories import SqlAlchemyRequestRepository
from tests._support import memory_session


async def _seed_trigger(session, *, workspace_id: uuid.UUID, key: str) -> uuid.UUID:
    row = TriggerEventRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        source="direct",
        trigger_kind=TriggerKind.DIRECT,
        idempotency_key=key,
        payload={},
        received_at=datetime.now(tz=UTC),
    )
    session.add(row)
    await session.flush()
    return row.id


@pytest.mark.asyncio
async def test_add_and_get_request_roundtrip() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        trig_id = await _seed_trigger(session, workspace_id=workspace_id, key="k1")

        repo = SqlAlchemyRequestRepository(session)
        request = RequestRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            trigger_event_id=trig_id,
            status=RequestStatus.OPEN,
            payload={"text": "hi"},
        )
        await repo.add(request)
        await session.flush()

        loaded = await repo.get(request.id)
        assert loaded is not None
        assert loaded.id == request.id
        assert loaded.workspace_id == workspace_id
        assert loaded.status == RequestStatus.OPEN


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyRequestRepository(session)
        assert await repo.get(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_list_by_workspace_scoped_and_ordered() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        sibling = uuid.uuid4()
        trig_a = await _seed_trigger(session, workspace_id=workspace_id, key="a")
        trig_b = await _seed_trigger(session, workspace_id=sibling, key="b")

        repo = SqlAlchemyRequestRepository(session)
        now = datetime.now(tz=UTC)
        ids = []
        for i in range(3):
            r_id = uuid.uuid4()
            ids.append(r_id)
            await repo.add(
                RequestRow(
                    id=r_id,
                    workspace_id=workspace_id,
                    trigger_event_id=trig_a,
                    status=RequestStatus.OPEN,
                    payload={},
                    created_at=now - timedelta(minutes=2 - i),
                    updated_at=now,
                )
            )
        # sibling-workspace request should not appear
        await repo.add(
            RequestRow(
                id=uuid.uuid4(),
                workspace_id=sibling,
                trigger_event_id=trig_b,
                status=RequestStatus.OPEN,
                payload={},
            )
        )
        await session.flush()

        rows = await repo.list_by_workspace(workspace_id)
        assert {r.workspace_id for r in rows} == {workspace_id}
        assert len(rows) == 3


@pytest.mark.asyncio
async def test_list_open_for_claim_filters_status_and_orders_oldest_first() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        trig = await _seed_trigger(session, workspace_id=workspace_id, key="c")

        repo = SqlAlchemyRequestRepository(session)
        now = datetime.now(tz=UTC)
        open_ids = []
        for i in range(3):
            r_id = uuid.uuid4()
            open_ids.append(r_id)
            await repo.add(
                RequestRow(
                    id=r_id,
                    workspace_id=workspace_id,
                    trigger_event_id=trig,
                    status=RequestStatus.OPEN,
                    payload={},
                    created_at=now - timedelta(minutes=3 - i),
                    updated_at=now,
                )
            )
        # non-OPEN row should not appear
        await repo.add(
            RequestRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                trigger_event_id=trig,
                status=RequestStatus.RUNNING,
                payload={},
                created_at=now - timedelta(minutes=10),
                updated_at=now,
            )
        )
        await session.flush()

        rows = await repo.list_open_for_claim(limit=10)
        # All returned should be OPEN
        assert all(r.status == RequestStatus.OPEN for r in rows)
        # Sorted oldest-first
        assert [r.id for r in rows] == open_ids


@pytest.mark.asyncio
async def test_list_open_for_claim_respects_limit() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        trig = await _seed_trigger(session, workspace_id=workspace_id, key="d")

        repo = SqlAlchemyRequestRepository(session)
        for _ in range(5):
            await repo.add(
                RequestRow(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    trigger_event_id=trig,
                    status=RequestStatus.OPEN,
                    payload={},
                )
            )
        await session.flush()

        rows = await repo.list_open_for_claim(limit=2)
        assert len(rows) == 2
