"""Focused unit tests for :class:`ExecutorOrchestrator` (Lift 5b).

These exercise the orchestrator directly (no AgentRunner, no _factory) against
an in-memory SQLite session + a ``fakeredis`` double — the timeout path and the
malformed-pinned-worker-id parse that the glue e2e doesn't reach.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

# Register the executor tables on the shared Base.metadata for create_all.
import backend.executors.db  # noqa: F401
from backend.accounts.models import ModelAccount
from backend.config import Settings
from backend.execution.db import (
    Decision,
    ExecutionRun,
    RunAttempt,
    RunAttemptPhase,
    RunStatus,
    WorkStep,
    WorkStepStatus,
)
from backend.executors import orchestrator as orch
from backend.executors.db import ExecutorTaskRow, WorkerRow
from backend.executors.orchestrator import ExecutorOrchestrator, _parse_uuid

from .._support import memory_session

pytestmark = pytest.mark.asyncio


async def _make_redis() -> Any:
    try:
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:  # pragma: no cover - declared dep
        pytest.skip("fakeredis not installed")
    client = fakeredis_aio.FakeRedis(decode_responses=True)
    await client.flushdb()
    return client


async def _seed(s: Any, *, executor_type: str = "claude_code") -> tuple[ExecutionRun, ModelAccount]:
    workspace_id = uuid.uuid4()
    worker = WorkerRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name="w",
        labels=[],
        capabilities=[executor_type],
        status="online",
        last_heartbeat=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        token_hash="0" * 64,
        is_active=True,
    )
    s.add(worker)
    account = ModelAccount(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        account_id=uuid.uuid4(),
        provider="executor",
        label="w",
        litellm_model=f"executor/{executor_type}",
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="unknown",
        is_active=True,
        extra_params={"worker_id": str(worker.id), "executor_type": executor_type},
    )
    s.add(account)
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={"intent_text": "do work"},
    )
    s.add(run)
    await s.flush()
    return run, account


async def test_parse_uuid_variants() -> None:
    u = uuid.uuid4()
    assert _parse_uuid(u) == u
    assert _parse_uuid(str(u)) == u
    assert _parse_uuid("not-a-uuid") is None
    assert _parse_uuid(None) is None
    assert _parse_uuid(12345) is None


async def test_timeout_yields_system_error(tmp_path: Path) -> None:
    redis = await _make_redis()
    async with memory_session() as s:
        run, account = await _seed(s)
        await s.commit()
        # A tiny timeout + a worker that never reports → TaskTimeout → system_error.
        settings = Settings(executor_task_timeout_s=0.05)
        oc = ExecutorOrchestrator(session=s, redis=redis, account=account, settings=settings)
        result = await oc.run(run=run, workspace_dir=tmp_path)
        await s.commit()

    assert result.outcome == "system_error"
    assert "timed out" in result.summary
    await redis.aclose()


async def test_timeout_marks_workstep_and_attempt_failed(tmp_path: Path) -> None:
    from sqlalchemy import select

    redis = await _make_redis()
    async with memory_session() as s:
        run, account = await _seed(s)
        await s.commit()
        settings = Settings(executor_task_timeout_s=0.05)
        oc = ExecutorOrchestrator(session=s, redis=redis, account=account, settings=settings)
        result = await oc.run(run=run, workspace_dir=tmp_path)
        await s.flush()

        assert result.outcome == "system_error"
        step = (await s.execute(select(WorkStep))).scalar_one()
        attempt = (await s.execute(select(RunAttempt))).scalar_one()
        assert step.status is WorkStepStatus.FAILED
        assert attempt.phase is RunAttemptPhase.FAILED
        assert attempt.finished_at is not None
        # No deliverable, no decision.
        assert (await s.execute(select(Decision))).first() is None
    await redis.aclose()


async def test_dispatched_task_does_not_carry_backend_absolute_path(tmp_path: Path) -> None:
    from sqlalchemy import select

    # ``tmp_path`` stands in for the backend container's /app/var/runs/<run_id>
    # path. It is meaningless to a remote worker, so the dispatched task must NOT
    # carry it — the worker manages its own per-task local dir now.
    redis = await _make_redis()
    async with memory_session() as s:
        run, account = await _seed(s)
        await s.commit()
        settings = Settings(executor_task_timeout_s=0.05)
        oc = ExecutorOrchestrator(session=s, redis=redis, account=account, settings=settings)
        await oc.run(run=run, workspace_dir=tmp_path)
        await s.flush()

        task = (await s.execute(select(ExecutorTaskRow))).scalar_one()
        assert task.workspace_dir != str(tmp_path)
        assert task.workspace_dir == "."
    await redis.aclose()


async def test_module_exports_decision_kinds() -> None:
    assert orch.DECISION_NO_WORKER_AVAILABLE == "no_executor_worker_available"
    assert orch.DECISION_NO_DISPATCH_TRANSPORT == "no_executor_dispatch_transport"
