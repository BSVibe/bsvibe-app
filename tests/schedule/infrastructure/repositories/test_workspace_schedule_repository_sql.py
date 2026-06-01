"""Lift I-Repo-Final Phase B — SqlAlchemyWorkspaceScheduleRepository tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.schedule.infrastructure.repositories import (
    SqlAlchemyWorkspaceScheduleRepository,
)
from backend.schedule.infrastructure.schedule_db import WorkspaceScheduleRow
from tests._support import memory_session


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.mark.asyncio
async def test_claim_due_returns_only_enabled_due_rows() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyWorkspaceScheduleRepository(session)
        now = _now()

        due_enabled = WorkspaceScheduleRow(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            plugin_name="plug-due",
            cron_expr="@hourly",
            next_run_at=now - timedelta(seconds=1),
            enabled=True,
        )
        due_disabled = WorkspaceScheduleRow(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            plugin_name="plug-disabled",
            cron_expr="@hourly",
            next_run_at=now - timedelta(seconds=1),
            enabled=False,
        )
        future_enabled = WorkspaceScheduleRow(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            plugin_name="plug-future",
            cron_expr="@hourly",
            next_run_at=now + timedelta(hours=1),
            enabled=True,
        )
        for row in (due_enabled, due_disabled, future_enabled):
            session.add(row)
        await session.flush()

        rows = await repo.claim_due(now=now)
        plugins = {r.plugin_name for r in rows}
        assert "plug-due" in plugins
        assert "plug-disabled" not in plugins
        assert "plug-future" not in plugins


@pytest.mark.asyncio
async def test_claim_due_orders_oldest_first() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyWorkspaceScheduleRepository(session)
        now = _now()
        older = WorkspaceScheduleRow(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            plugin_name="older",
            cron_expr="@hourly",
            next_run_at=now - timedelta(seconds=10),
            enabled=True,
        )
        newer = WorkspaceScheduleRow(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            plugin_name="newer",
            cron_expr="@hourly",
            next_run_at=now - timedelta(seconds=1),
            enabled=True,
        )
        # Inserted out of order on purpose.
        session.add(newer)
        session.add(older)
        await session.flush()

        rows = await repo.claim_due(now=now)
        assert [r.plugin_name for r in rows] == ["older", "newer"]


@pytest.mark.asyncio
async def test_advance_flips_next_and_last_fired() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyWorkspaceScheduleRepository(session)
        now = _now()
        row = WorkspaceScheduleRow(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            plugin_name="p",
            cron_expr="@hourly",
            next_run_at=now,
            enabled=True,
        )
        session.add(row)
        await session.flush()

        new_next = now + timedelta(hours=1)
        returned = await repo.advance(row, next_run_at=new_next, last_fired_at=now)
        assert returned is row
        assert row.next_run_at == new_next
        assert row.last_fired_at == now


@pytest.mark.asyncio
async def test_repository_does_not_commit() -> None:
    """``advance`` must NOT commit — the runner owns the txn."""
    async with memory_session() as session:
        repo = SqlAlchemyWorkspaceScheduleRepository(session)
        now = _now()
        row = WorkspaceScheduleRow(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            plugin_name="p",
            cron_expr="@hourly",
            next_run_at=now,
            enabled=True,
        )
        session.add(row)
        await session.flush()
        await repo.advance(row, next_run_at=now + timedelta(hours=1), last_fired_at=now)
        assert session.in_transaction() is True
