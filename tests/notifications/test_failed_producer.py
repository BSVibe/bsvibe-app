"""Producer-existence proof for the ``failed`` outbox event (Notifier N3).

[P-failed] drives the SINGLE run-terminal-FAILED funnel —
``AgentRunner.transition(to_status=RunStatus.FAILED)`` (both production FAILED
writes route through it) — against a real DB and asserts a real
``NotificationEventRow(event="failed", dedupe_key="failed:<run_id>")`` lands. A
non-FAILED transition emits nothing; a repeated FAILED transition is a no-op
(terminal), so the founder is told exactly once.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import backend.notifications.db  # noqa: F401 — register table on the shared Base
from backend.notifications.db import NotificationEventRow, NotificationStatus
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import memory_session

pytestmark = pytest.mark.asyncio


async def _seed_run(s, *, status: RunStatus = RunStatus.RUNNING) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=uuid.uuid4(),
        status=status,
        payload={},
    )
    s.add(run)
    await s.flush()
    return run


async def _failed_rows(s, ws: uuid.UUID) -> list[NotificationEventRow]:
    return list(
        (
            await s.execute(
                select(NotificationEventRow).where(
                    NotificationEventRow.workspace_id == ws,
                    NotificationEventRow.event == "failed",
                )
            )
        )
        .scalars()
        .all()
    )


async def test_run_terminal_failed_emits_failed() -> None:
    """[P-failed] a run driven to terminal FAILED queues a ``failed`` notification."""
    async with memory_session() as s:
        run = await _seed_run(s)
        await AgentRunner(s).transition(
            run_id=run.id, to_status=RunStatus.FAILED, reason="frame could not classify"
        )
        await s.commit()

        rows = await _failed_rows(s, run.workspace_id)
        assert len(rows) == 1, "the founder was never told the run failed"
        row = rows[0]
        assert row.dedupe_key == f"failed:{run.id}"
        assert row.status is NotificationStatus.PENDING
        assert row.payload["run_id"] == str(run.id)
        assert row.payload["link"] == f"/runs/{run.id}"
        assert "frame could not classify" in row.payload["body"]


async def test_non_failed_transition_does_not_emit_failed() -> None:
    """Transitioning to a non-terminal-FAILED status emits no ``failed`` row."""
    async with memory_session() as s:
        run = await _seed_run(s, status=RunStatus.OPEN)
        await AgentRunner(s).transition(run_id=run.id, to_status=RunStatus.RUNNING)
        await s.commit()

        assert await _failed_rows(s, run.workspace_id) == []


async def test_repeated_failed_transition_notifies_once() -> None:
    """[D] FAILED is terminal — a repeated transition no-ops → exactly one row."""
    async with memory_session() as s:
        run = await _seed_run(s)
        runner = AgentRunner(s)
        await runner.transition(run_id=run.id, to_status=RunStatus.FAILED, reason="boom")
        await runner.transition(run_id=run.id, to_status=RunStatus.FAILED, reason="boom again")
        await s.commit()

        assert len(await _failed_rows(s, run.workspace_id)) == 1
