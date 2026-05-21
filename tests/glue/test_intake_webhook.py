"""Intake WebhookReceiver — idempotency + persistence against real PG."""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.intake.db import IntakeBase, TriggerEventRow
from backend.intake.webhook import WebhookReceiver

PG_URL = os.environ.get(
    "BSVIBE_DATABASE_URL", "postgresql+asyncpg://bsvibe:bsvibe@localhost:5442/bsvibe"
)


pytestmark = pytest.mark.asyncio


async def _can_reach_pg() -> bool:
    try:
        engine = create_async_engine(PG_URL, future=True, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    use_pg = os.environ.get("BSVIBE_DATABASE_URL") and await _can_reach_pg()
    url = PG_URL if use_pg else "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(IntakeBase.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        yield s
    if use_pg:
        async with engine.begin() as conn:
            await conn.run_sync(IntakeBase.metadata.drop_all)
    await engine.dispose()


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
