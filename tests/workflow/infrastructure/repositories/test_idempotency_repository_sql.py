"""Lift I-Repo-Workflow-3 — SqlAlchemyIdempotencyRepository round-trip tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.repositories import SqlAlchemyIdempotencyRepository
from tests._support import memory_session


def _trigger(
    *,
    workspace_id: uuid.UUID,
    source: str,
    key: str,
    kind: TriggerKind = TriggerKind.WEBHOOK,
) -> TriggerEventRow:
    return TriggerEventRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        source=source,
        trigger_kind=kind,
        idempotency_key=key,
        payload={},
        received_at=datetime.now(tz=UTC),
    )


@pytest.mark.asyncio
async def test_is_duplicate_false_when_empty() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyIdempotencyRepository(session)
        assert (
            await repo.is_duplicate(
                workspace_id=uuid.uuid4(), source="github", idempotency_key="nope"
            )
            is False
        )


@pytest.mark.asyncio
async def test_record_and_is_duplicate_true() -> None:
    async with memory_session() as session:
        workspace_id = uuid.uuid4()
        repo = SqlAlchemyIdempotencyRepository(session)
        row = _trigger(workspace_id=workspace_id, source="github", key="abc")
        await repo.record(row)
        await session.flush()

        assert (
            await repo.is_duplicate(
                workspace_id=workspace_id, source="github", idempotency_key="abc"
            )
            is True
        )


@pytest.mark.asyncio
async def test_is_duplicate_scoped_by_workspace_and_source() -> None:
    async with memory_session() as session:
        ws_a = uuid.uuid4()
        ws_b = uuid.uuid4()
        repo = SqlAlchemyIdempotencyRepository(session)
        await repo.record(_trigger(workspace_id=ws_a, source="github", key="k"))
        await session.flush()

        # Different workspace → not a dup.
        assert (
            await repo.is_duplicate(workspace_id=ws_b, source="github", idempotency_key="k")
            is False
        )
        # Different source → not a dup.
        assert (
            await repo.is_duplicate(workspace_id=ws_a, source="linear", idempotency_key="k")
            is False
        )
        # Same triple → dup.
        assert (
            await repo.is_duplicate(workspace_id=ws_a, source="github", idempotency_key="k") is True
        )


@pytest.mark.asyncio
async def test_list_undrained_excludes_triggers_with_request() -> None:
    async with memory_session() as session:
        ws = uuid.uuid4()
        repo = SqlAlchemyIdempotencyRepository(session)
        drained = _trigger(workspace_id=ws, source="github", key="drained")
        undrained = _trigger(workspace_id=ws, source="github", key="undrained")
        await repo.record(drained)
        await repo.record(undrained)
        session.add(
            RequestRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                trigger_event_id=drained.id,
                status=RequestStatus.OPEN,
                payload={},
            )
        )
        await session.flush()

        rows = await repo.list_undrained(limit=10)
        ids = {r.id for r in rows}
        assert undrained.id in ids
        assert drained.id not in ids


@pytest.mark.asyncio
async def test_list_undrained_respects_limit_and_ordering() -> None:
    async with memory_session() as session:
        ws = uuid.uuid4()
        repo = SqlAlchemyIdempotencyRepository(session)
        from datetime import timedelta

        base = datetime.now(tz=UTC)
        keys = []
        for i in range(4):
            row = _trigger(workspace_id=ws, source="github", key=f"k{i}")
            row.received_at = base - timedelta(minutes=4 - i)
            keys.append(row.id)
            await repo.record(row)
        await session.flush()

        rows = await repo.list_undrained(limit=2)
        # oldest-first by received_at
        assert [r.id for r in rows] == keys[:2]
