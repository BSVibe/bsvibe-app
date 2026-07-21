"""PT2 — a ``product_tick`` schedule fire seeds a localized meta-instruction.

The founder sets the cadence (``kind=product_tick``); BSVibe decides WHAT to do.
So the emitter must NOT carry the (unused) schedule ``text`` — it seeds a
localized "decide + do the next action for THIS product" meta-instruction so the
run frames a real task (not "Untitled run") and the agent decides+acts.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Imported for table registration on the shared Base.metadata.
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.intake.db  # noqa: F401
from backend.identity.workspaces_db import WorkspaceRow
from backend.schedule.application.emitter import ScheduleTrigger
from backend.schedule.domain.product_tick import product_tick_instruction
from backend.workflow.infrastructure.intake.db import TriggerEventRow
from backend.workflow.infrastructure.workers.agent_worker import _request_intent_text
from tests._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def test_product_tick_instruction_localized() -> None:
    """The meta-instruction is non-empty in both languages and differs by locale."""
    en = product_tick_instruction("en")
    ko = product_tick_instruction("ko")
    assert en.strip()
    assert ko.strip()
    assert en != ko
    # A missing / unknown language degrades to English (never empty).
    assert product_tick_instruction(None) == en
    assert product_tick_instruction("zz") == en


async def test_fire_product_tick_seeds_meta_instruction(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    ws = uuid.uuid4()
    product_id = uuid.uuid4()
    schedule_id = uuid.uuid4()
    fired_at = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)
    async with sf() as session:
        session.add(WorkspaceRow(id=ws, name="acme"))  # default language en
        await session.commit()

        trigger = ScheduleTrigger(session)
        outcome = await trigger.fire(
            workspace_id=ws,
            schedule_id=schedule_id,
            kind="product_tick",
            # The stored schedule payload is empty for product_tick — the emitter
            # must NOT depend on a founder-provided ``text``.
            schedule_payload={},
            cron_expr="0 9 * * *",
            fired_at=fired_at,
            product_id=product_id,
        )
        assert outcome.duplicate is False
        await session.commit()

    async with sf() as session:
        row = (await session.execute(select(TriggerEventRow))).scalar_one()
        payload = row.payload
        assert payload["kind"] == "product_tick"
        assert payload["text"] == product_tick_instruction("en")
        assert payload["text"].strip()  # non-empty meta-instruction
        assert row.product_id == product_id


async def test_fire_product_tick_localizes_to_workspace_language(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    ws = uuid.uuid4()
    async with sf() as session:
        session.add(WorkspaceRow(id=ws, name="acme", language="ko"))
        await session.commit()

        trigger = ScheduleTrigger(session)
        await trigger.fire(
            workspace_id=ws,
            schedule_id=uuid.uuid4(),
            kind="product_tick",
            schedule_payload={},
            cron_expr="0 9 * * *",
            fired_at=datetime(2026, 7, 22, 9, 0, tzinfo=UTC),
            product_id=uuid.uuid4(),
        )
        await session.commit()

    async with sf() as session:
        row = (await session.execute(select(TriggerEventRow))).scalar_one()
        assert row.payload["text"] == product_tick_instruction("ko")


async def test_fire_instruction_kind_still_uses_schedule_text(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """No regression — the instruction kind still carries its founder ``text``."""
    ws = uuid.uuid4()
    async with sf() as session:
        session.add(WorkspaceRow(id=ws, name="acme"))
        await session.commit()

        trigger = ScheduleTrigger(session)
        await trigger.fire(
            workspace_id=ws,
            schedule_id=uuid.uuid4(),
            kind="instruction",
            schedule_payload={"text": "post the weekly summary"},
            cron_expr="0 9 * * 1",
            fired_at=datetime(2026, 7, 27, 9, 0, tzinfo=UTC),
        )
        await session.commit()

    async with sf() as session:
        row = (await session.execute(select(TriggerEventRow))).scalar_one()
        assert row.payload["text"] == "post the weekly summary"
        assert row.payload["kind"] == "instruction"


async def test_framer_intent_reads_tick_meta_instruction() -> None:
    """Integration — the framer's intent extraction returns the meta-instruction
    (NOT "Untitled run") for a product_tick-seeded request payload."""

    class _Req:
        payload = {"text": product_tick_instruction("en"), "kind": "product_tick"}

    intent = _request_intent_text(_Req())  # type: ignore[arg-type]
    assert intent == product_tick_instruction("en")
    assert intent != "Untitled run"
