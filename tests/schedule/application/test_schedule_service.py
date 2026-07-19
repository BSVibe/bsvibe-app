"""ScheduleService — the authoring producer (S1) unit tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from backend.schedule.application.schedule_service import (
    ScheduleService,
    ScheduleValidationError,
)
from tests._support import memory_session

pytestmark = pytest.mark.asyncio


async def test_create_persists_instruction_row_with_text_payload() -> None:
    async with memory_session() as session:
        service = ScheduleService(session)
        ws = uuid.uuid4()
        now = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)  # Wednesday
        row = await service.create(
            workspace_id=ws,
            kind="instruction",
            text="post the weekly market summary",
            cron_expr="0 9 * * 1",
            title="Weekly summary",
            now=now,
        )
        await session.commit()

        assert row.kind == "instruction"
        assert row.payload == {"text": "post the weekly market summary"}
        assert row.plugin_name is None
        assert row.enabled is True
        # First next_run_at is the next Monday 09:00 UTC.
        assert row.next_run_at == datetime(2026, 7, 27, 9, 0, tzinfo=UTC)

        listed = await service.list(workspace_id=ws)
        assert [r.id for r in listed] == [row.id]


async def test_create_rejects_invalid_cron() -> None:
    async with memory_session() as session:
        service = ScheduleService(session)
        with pytest.raises(ScheduleValidationError):
            await service.create(
                workspace_id=uuid.uuid4(),
                kind="instruction",
                text="do a thing",
                cron_expr="not a valid cron",
            )


async def test_create_rejects_unsupported_kind() -> None:
    async with memory_session() as session:
        service = ScheduleService(session)
        with pytest.raises(ScheduleValidationError):
            await service.create(
                workspace_id=uuid.uuid4(),
                kind="plugin_action",
                text="do a thing",
                cron_expr="0 9 * * 1",
            )


async def test_create_rejects_empty_text() -> None:
    async with memory_session() as session:
        service = ScheduleService(session)
        with pytest.raises(ScheduleValidationError):
            await service.create(
                workspace_id=uuid.uuid4(),
                kind="instruction",
                text="   ",
                cron_expr="0 9 * * 1",
            )


async def test_delete_and_set_enabled_are_workspace_scoped() -> None:
    async with memory_session() as session:
        service = ScheduleService(session)
        ws = uuid.uuid4()
        other_ws = uuid.uuid4()
        row = await service.create(
            workspace_id=ws, kind="instruction", text="x", cron_expr="* * * * *"
        )
        await session.commit()

        # A foreign workspace cannot see, disable, or delete it.
        assert await service.get(schedule_id=row.id, workspace_id=other_ws) is None
        assert (
            await service.set_enabled(schedule_id=row.id, workspace_id=other_ws, enabled=False)
            is None
        )
        assert await service.delete(schedule_id=row.id, workspace_id=other_ws) is False

        # The owner can disable then delete it.
        disabled = await service.set_enabled(schedule_id=row.id, workspace_id=ws, enabled=False)
        assert disabled is not None and disabled.enabled is False
        await session.commit()
        assert await service.delete(schedule_id=row.id, workspace_id=ws) is True
        await session.commit()
        assert await service.list(workspace_id=ws) == []
