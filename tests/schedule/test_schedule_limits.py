"""Schedule limits: count cap, minimum interval, catch-up, overlap, deleted workspace."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from backend.config import get_settings
from backend.identity.workspaces_db import WorkspaceRow
from backend.schedule.application.schedule_service import (
    ScheduleService,
    ScheduleValidationError,
)
from backend.schedule.domain.advancer import CronScheduleAdvancer
from backend.schedule.infrastructure.db_poll_runner import DbPollScheduleRunner
from backend.schedule.infrastructure.schedule_db import WorkspaceScheduleRow
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    TriggerEventRow,
)
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


class _SessionFactory:
    """Hands the runner the SAME session the test seeds, without closing it."""

    def __init__(self, session) -> None:  # noqa: ANN001
        self._session = session

    def __call__(self):  # noqa: ANN204
        return self

    async def __aenter__(self):  # noqa: ANN204
        return self._session

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        return None


def _runner(now: datetime) -> DbPollScheduleRunner:
    return DbPollScheduleRunner(advancer=CronScheduleAdvancer(), now_fn=lambda: now)


async def _seed_schedule(session, workspace_id: uuid.UUID, *, next_run_at: datetime):  # noqa: ANN001
    row = WorkspaceScheduleRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        kind="instruction",
        payload={"text": "do it"},
        cron_expr="*/15 * * * *",
        next_run_at=next_run_at,
        enabled=True,
    )
    session.add(row)
    await session.flush()
    return row


async def _fire(session, now: datetime) -> int:  # noqa: ANN001
    return await _runner(now).fire_due(session_factory=_SessionFactory(session), now=now)


async def _trigger_events(session, schedule_id: uuid.UUID) -> list[TriggerEventRow]:  # noqa: ANN001
    rows = await session.execute(
        select(TriggerEventRow).where(TriggerEventRow.idempotency_key.like(f"{schedule_id}:%"))
    )
    return list(rows.scalars().all())


# -- R1 / R2: authoring limits -------------------------------------------------


async def test_create_over_the_workspace_cap_is_refused() -> None:
    cap = get_settings().schedule_max_per_workspace
    async with memory_session() as session:
        service = ScheduleService(session)
        ws = uuid.uuid4()
        for _ in range(cap):
            await service.create(
                workspace_id=ws, kind="instruction", text="x", cron_expr="0 9 * * 1", now=_NOW
            )
        with pytest.raises(ScheduleValidationError, match="too many schedules"):
            await service.create(
                workspace_id=ws, kind="instruction", text="x", cron_expr="0 9 * * 1", now=_NOW
            )
        # Another workspace is unaffected.
        await service.create(
            workspace_id=uuid.uuid4(),
            kind="instruction",
            text="x",
            cron_expr="0 9 * * 1",
            now=_NOW,
        )


async def test_every_minute_cron_is_refused() -> None:
    async with memory_session() as session:
        with pytest.raises(ScheduleValidationError, match="too often"):
            await ScheduleService(session).create(
                workspace_id=uuid.uuid4(),
                kind="instruction",
                text="x",
                cron_expr="* * * * *",
                now=_NOW,
            )


async def test_irregular_cron_with_a_short_gap_is_refused_and_a_quarter_hour_is_fine() -> None:
    async with memory_session() as session:
        service = ScheduleService(session)
        ws = uuid.uuid4()
        # 0,10,20..50 → 10 min apart; 50→60 also 10.
        with pytest.raises(ScheduleValidationError, match="too often"):
            await service.create(
                workspace_id=ws, kind="instruction", text="x", cron_expr="*/10 * * * *", now=_NOW
            )
        # Gap only at the hour wrap: 0 and 5 minutes past.
        with pytest.raises(ScheduleValidationError, match="too often"):
            await service.create(
                workspace_id=ws, kind="instruction", text="x", cron_expr="0,5 * * * *", now=_NOW
            )
        await service.create(
            workspace_id=ws, kind="instruction", text="x", cron_expr="*/15 * * * *", now=_NOW
        )


# -- R3: catch-up fires once ---------------------------------------------------


async def test_an_hour_behind_schedule_fires_once_and_advances_from_now() -> None:
    async with memory_session() as session:
        ws = uuid.uuid4()
        # Due an hour ago; a 15-minute cron has ~4 missed windows.
        sched = await _seed_schedule(session, ws, next_run_at=_NOW - timedelta(hours=1))
        schedule_id = sched.id

        assert await _fire(session, _NOW) == 1
        assert len(await _trigger_events(session, schedule_id)) == 1
        assert sched.next_run_at == datetime(2026, 11, 14, 12, 15, tzinfo=UTC)

        # Immediately polling again fires nothing more.
        assert await _fire(session, _NOW + timedelta(seconds=10)) == 0
        assert len(await _trigger_events(session, schedule_id)) == 1


# -- R4: no overlap ------------------------------------------------------------


async def _seed_run_from_schedule(
    session,  # noqa: ANN001
    sched: WorkspaceScheduleRow,
    status: RunStatus,
) -> None:
    trigger_id, request_id = uuid.uuid4(), uuid.uuid4()
    session.add(
        TriggerEventRow(
            id=trigger_id,
            workspace_id=sched.workspace_id,
            source="schedule",
            trigger_kind="schedule",
            idempotency_key=f"{sched.id}:{(_NOW - timedelta(minutes=15)).isoformat()}",
            payload={"schedule_id": str(sched.id)},
            received_at=_NOW - timedelta(minutes=15),
        )
    )
    await session.flush()
    session.add(
        RequestRow(id=request_id, workspace_id=sched.workspace_id, trigger_event_id=trigger_id)
    )
    await session.flush()
    session.add(
        ExecutionRun(
            id=uuid.uuid4(), workspace_id=sched.workspace_id, request_id=request_id, status=status
        )
    )
    await session.flush()


async def test_a_schedule_with_a_run_in_flight_skips_this_window() -> None:
    async with memory_session() as session:
        sched = await _seed_schedule(session, uuid.uuid4(), next_run_at=_NOW - timedelta(seconds=5))
        schedule_id = sched.id
        await _seed_run_from_schedule(session, sched, RunStatus.RUNNING)
        before = len(await _trigger_events(session, schedule_id))

        assert await _fire(session, _NOW) == 0
        assert len(await _trigger_events(session, schedule_id)) == before
        # The window is consumed, not retried every 10s.
        assert sched.next_run_at == datetime(2026, 11, 14, 12, 15, tzinfo=UTC)


async def test_a_finished_run_does_not_block_the_next_window() -> None:
    async with memory_session() as session:
        sched = await _seed_schedule(session, uuid.uuid4(), next_run_at=_NOW - timedelta(seconds=5))
        schedule_id = sched.id
        await _seed_run_from_schedule(session, sched, RunStatus.SHIPPED)
        before = len(await _trigger_events(session, schedule_id))

        assert await _fire(session, _NOW) == 1
        assert len(await _trigger_events(session, schedule_id)) == before + 1


# -- R5: deleted workspace -----------------------------------------------------


async def test_a_deleted_workspaces_schedule_does_not_fire() -> None:
    async with memory_session() as session:
        ws = WorkspaceRow(id=uuid.uuid4(), name="gone", deleted_at=_NOW - timedelta(days=1))
        session.add(ws)
        await session.flush()
        sched = await _seed_schedule(session, ws.id, next_run_at=_NOW - timedelta(seconds=5))
        schedule_id = sched.id

        assert await _fire(session, _NOW) == 0
        assert await _trigger_events(session, schedule_id) == []


async def test_deleting_a_workspace_disables_its_schedules() -> None:
    async with memory_session() as session:
        ws = uuid.uuid4()
        other = uuid.uuid4()
        mine = await _seed_schedule(session, ws, next_run_at=_NOW)
        theirs = await _seed_schedule(session, other, next_run_at=_NOW)

        assert await ScheduleService(session).disable_for_workspace(workspace_id=ws) == 1
        assert mine.enabled is False
        assert theirs.enabled is True


# -- re-enable goes through the same limits --------------------------------------


async def test_enabling_a_disabled_schedule_at_the_cap_is_refused() -> None:
    cap = get_settings().schedule_max_per_workspace
    async with memory_session() as session:
        service = ScheduleService(session)
        ws = uuid.uuid4()
        for _ in range(cap):
            await _seed_schedule(session, ws, next_run_at=_NOW)
        off = await _seed_schedule(session, ws, next_run_at=_NOW)
        off.enabled = False
        await session.flush()
        with pytest.raises(ScheduleValidationError, match="too many schedules"):
            await service.set_enabled(schedule_id=off.id, workspace_id=ws, enabled=True)
        # Already-enabled stays a passing no-op.
        enabled = await service.list(workspace_id=ws)
        on = next(r for r in enabled if r.enabled)
        assert (
            await service.set_enabled(schedule_id=on.id, workspace_id=ws, enabled=True)
        ) is not None


async def test_enabling_a_disabled_every_minute_schedule_is_refused() -> None:
    async with memory_session() as session:
        ws = uuid.uuid4()
        row = await _seed_schedule(session, ws, next_run_at=_NOW)
        row.cron_expr = "* * * * *"
        row.enabled = False
        await session.flush()
        with pytest.raises(ScheduleValidationError, match="too often"):
            await ScheduleService(session).set_enabled(
                schedule_id=row.id, workspace_id=ws, enabled=True
            )
