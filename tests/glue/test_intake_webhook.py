"""Intake WebhookReceiver — idempotency + persistence against real PG."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.intake.db import IntakeBase, TriggerEventRow
from backend.intake.webhook import WebhookReceiver

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    async with db_engine(IntakeBase) as (engine, _is_pg):
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            yield s


async def test_webhook_inserts_trigger_event(session: AsyncSession) -> None:
    receiver = WebhookReceiver(session)
    ws = uuid.uuid4()
    outcome = await receiver.handle(
        workspace_id=ws,
        source="github",
        headers={"X-GitHub-Delivery": "abc-123"},
        body={"action": "opened", "pull_request": {"id": 42}},
    )
    assert outcome.duplicate is False
    assert outcome.event.source == "github"
    assert outcome.event.idempotency_key == "abc-123"
    await session.commit()

    from sqlalchemy import select

    res = await session.execute(select(TriggerEventRow).where(TriggerEventRow.workspace_id == ws))
    rows = res.scalars().all()
    assert len(rows) == 1
    assert rows[0].idempotency_key == "abc-123"


async def test_webhook_dedup_same_key(session: AsyncSession) -> None:
    receiver = WebhookReceiver(session)
    ws = uuid.uuid4()
    headers = {"X-GitHub-Delivery": "same-key"}
    body = {"hello": "world"}
    a = await receiver.handle(workspace_id=ws, source="github", headers=headers, body=body)
    await session.commit()
    b = await receiver.handle(workspace_id=ws, source="github", headers=headers, body=body)
    assert a.duplicate is False
    assert b.duplicate is True


async def test_webhook_body_hash_fallback(session: AsyncSession) -> None:
    receiver = WebhookReceiver(session)
    ws = uuid.uuid4()
    body = {"some": "payload"}
    outcome = await receiver.handle(workspace_id=ws, source="custom", headers={}, body=body)
    # 64-char sha256 hex digest
    assert len(outcome.event.idempotency_key) == 64
    await session.commit()


async def test_workspace_isolation(session: AsyncSession) -> None:
    receiver = WebhookReceiver(session)
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    body = {"x": 1}
    headers = {"X-Idempotency-Key": "shared"}
    a = await receiver.handle(workspace_id=ws_a, source="x", headers=headers, body=body)
    await session.commit()
    # Same key, different workspace → NOT a duplicate.
    b = await receiver.handle(workspace_id=ws_b, source="x", headers=headers, body=body)
    assert a.duplicate is False
    assert b.duplicate is False
