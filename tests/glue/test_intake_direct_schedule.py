"""DirectTrigger + ScheduleTrigger — idempotent intake against real PG."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.schedule.application.emitter import ScheduleTrigger
from backend.workflow.application.intake.direct import DirectTrigger
from backend.workflow.infrastructure.intake.db import IntakeBase, TriggerEventRow

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    async with db_engine(IntakeBase) as (engine, _is_pg):
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            yield s


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
