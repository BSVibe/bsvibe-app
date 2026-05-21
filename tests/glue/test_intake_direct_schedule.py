"""DirectTrigger + ScheduleTrigger — idempotent intake against real PG."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.intake.db import IntakeBase, TriggerEventRow
from backend.intake.direct import DirectTrigger
from backend.intake.schedule import ScheduleTrigger

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
    if not await _can_reach_pg():
        pytest.skip(f"Postgres not reachable at {PG_URL}")
    engine = create_async_engine(PG_URL, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(IntakeBase.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(IntakeBase.metadata.drop_all)
    await engine.dispose()


async def test_direct_submit_persists(session: AsyncSession) -> None:
    trigger = DirectTrigger(session)
    ws = uuid.uuid4()
    founder = uuid.uuid4()
    outcome = await trigger.submit(workspace_id=ws, founder_id=founder, text="Hello world")
    await session.commit()
    assert outcome.duplicate is False
    res = await session.execute(select(TriggerEventRow).where(TriggerEventRow.workspace_id == ws))
    rows = res.scalars().all()
    assert len(rows) == 1
    assert rows[0].source == "direct"


async def test_direct_submit_dedup_same_founder_same_text(session: AsyncSession) -> None:
    trigger = DirectTrigger(session)
    ws = uuid.uuid4()
    founder = uuid.uuid4()
    a = await trigger.submit(workspace_id=ws, founder_id=founder, text="same text")
    await session.commit()
    b = await trigger.submit(workspace_id=ws, founder_id=founder, text="same text")
    assert a.duplicate is False
    assert b.duplicate is True


async def test_direct_different_founders_no_dedup(session: AsyncSession) -> None:
    trigger = DirectTrigger(session)
    ws = uuid.uuid4()
    await trigger.submit(workspace_id=ws, founder_id=uuid.uuid4(), text="same text")
    await session.commit()
    b = await trigger.submit(workspace_id=ws, founder_id=uuid.uuid4(), text="same text")
    assert b.duplicate is False


async def test_schedule_fire_persists(session: AsyncSession) -> None:
    trigger = ScheduleTrigger(session)
    ws = uuid.uuid4()
    fired_at = datetime(2026, 5, 21, 9, 0, 0, tzinfo=UTC)
    outcome = await trigger.fire(
        workspace_id=ws,
        plugin_name="cron-summary",
        cron_expr="0 9 * * MON",
        fired_at=fired_at,
    )
    await session.commit()
    assert outcome.duplicate is False
    assert outcome.event.payload["cron_expr"] == "0 9 * * MON"


async def test_schedule_dedup_same_tick(session: AsyncSession) -> None:
    trigger = ScheduleTrigger(session)
    ws = uuid.uuid4()
    fired_at = datetime(2026, 5, 21, 9, 0, 0, tzinfo=UTC)
    a = await trigger.fire(workspace_id=ws, plugin_name="cron", cron_expr="*", fired_at=fired_at)
    await session.commit()
    b = await trigger.fire(workspace_id=ws, plugin_name="cron", cron_expr="*", fired_at=fired_at)
    assert a.duplicate is False
    assert b.duplicate is True
