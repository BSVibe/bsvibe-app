"""B15 — Executor-driven runs emit audit events into the supervisor outbox.

The ExecutorOrchestrator (Lift 5b / B2b) also drives a Workflow §11.3 run,
just through an external CLI worker instead of the native LLM loop. Before
B15 it emitted no audit events at all — the audit stream was blind to every
executor dispatch. These tests pin the minimum executor event set:

* ``RunStarted`` on dispatch (mirrors the native orchestrator).
* ``DecisionPending`` when dispatch can't proceed (no worker / no transport)
  or B2b verification cannot pass (no contract / no judge / contract FAIL).
* ``LoopTerminal`` on every exit path (verified / system_error /
  needs_decision).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

# Register executor tables on Base.metadata for create_all.
import backend.executors.db  # noqa: F401
from backend.config import Settings
from backend.execution.audit_events import (
    DecisionPending,
    LoopTerminal,
    RunStarted,
)
from backend.executors.orchestrator import ExecutorOrchestrator
from backend.supervisor.audit.models import AuditOutboxRecord

from .._support import memory_session
from .test_orchestrator import FakeBox, FakeSandboxManager, _make_redis, _seed

pytestmark = pytest.mark.asyncio


async def _outbox_types(session):
    rows = (
        (await session.execute(select(AuditOutboxRecord).order_by(AuditOutboxRecord.id.asc())))
        .scalars()
        .all()
    )
    return list(rows), [r.event_type for r in rows]


async def test_executor_timeout_emits_run_started_and_terminal_system_error(
    tmp_path: Path,
) -> None:
    """A timeout path emits RunStarted on dispatch + a LoopTerminal with
    outcome=system_error so the audit stream sees the bad terminal."""
    redis = await _make_redis()
    async with memory_session() as s:
        run, account = await _seed(s)
        await s.commit()
        settings = Settings(executor_task_timeout_s=0.05)
        oc = ExecutorOrchestrator(
            session=s,
            redis=redis,
            account=account,
            settings=settings,
            sandbox_manager=FakeSandboxManager(FakeBox()),
        )
        result = await oc.run(run=run, workspace_dir=tmp_path)
        await s.commit()
        assert result.outcome == "system_error"

        rows, types = await _outbox_types(s)
        assert RunStarted.DEFAULT_EVENT_TYPE in types
        assert types[-1] == LoopTerminal.DEFAULT_EVENT_TYPE
        assert rows[-1].payload["data"]["outcome"] == "system_error"
    await redis.aclose()


async def test_executor_no_dispatch_transport_emits_decision_pending(tmp_path: Path) -> None:
    """No Redis client → no_executor_dispatch_transport Decision; the outbox
    must see DecisionPending + a needs_decision LoopTerminal."""
    async with memory_session() as s:
        run, account = await _seed(s)
        await s.commit()
        oc = ExecutorOrchestrator(
            session=s,
            redis=None,
            account=account,
            settings=Settings(),
            sandbox_manager=FakeSandboxManager(FakeBox()),
        )
        result = await oc.run(run=run, workspace_dir=tmp_path)
        await s.commit()
        assert result.outcome == "needs_decision"

        rows, types = await _outbox_types(s)
        assert RunStarted.DEFAULT_EVENT_TYPE in types
        assert DecisionPending.DEFAULT_EVENT_TYPE in types
        assert types[-1] == LoopTerminal.DEFAULT_EVENT_TYPE
        pending = next(r for r in rows if r.event_type == DecisionPending.DEFAULT_EVENT_TYPE)
        assert pending.payload["data"]["kind"] == "no_executor_dispatch_transport"
