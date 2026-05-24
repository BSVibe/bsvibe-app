"""Executor run end-to-end — provider='executor' run dispatches to a worker.

Lift 5b of the executor-pool epic (Workflow §8.4 / §11.3). The KEYSTONE
integration: a run whose resolved ModelAccount is ``provider='executor'``
must NOT enter the native LLM loop — it must dispatch a task to a registered
external worker and, on the worker reporting success, produce the SAME
verified artifacts the native path produces (Deliverable type CODE +
DeliveryEventRow + settle activity), landing the run REVIEW_READY.

This drives the *real* :func:`backend.workers.run._factory` branch (so the
provider switch + ExecutorOrchestrator construction are exercised, not a
hand-built orchestrator) through :meth:`AgentRunner.drive`, and SIMULATES
the worker with ``fakeredis`` + :func:`dispatch.record_result` + a publish on
the done channel — exactly the shape of ``tests/executors/test_dispatch.py``.

Runs on in-memory SQLite by default, real Postgres when ``BSVIBE_DATABASE_URL``
is set (mirrors the other glue tests).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Importing the module dbs registers their tables on the shared Base.metadata.
import backend.executors.db  # noqa: F401
from backend.accounts.models import ModelAccount
from backend.config import get_settings
from backend.delivery.db import DeliveryEventRow
from backend.execution.db import (
    Decision,
    Deliverable,
    DeliverableType,
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
)
from backend.executors import dispatch
from backend.executors.db import WorkerRow
from backend.executors.orchestrator import ExecutorOrchestrator
from backend.orchestrator.agent_runner import AgentRunner
from backend.workers.run import build_agent_execution_deps

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _make_redis() -> Any:
    try:
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:  # pragma: no cover - fakeredis is a declared dep
        pytest.skip("fakeredis not installed")
    client = fakeredis_aio.FakeRedis(decode_responses=True)
    await client.flushdb()
    return client


async def _seed_worker(
    s: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    capabilities: list[str],
) -> WorkerRow:
    worker = WorkerRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name="mac-mini",
        labels=[],
        capabilities=list(capabilities),
        status="online",
        last_heartbeat=datetime.now(UTC) - timedelta(seconds=1),
        token_hash="0" * 64,
        is_active=True,
    )
    s.add(worker)
    await s.flush()
    return worker


async def _seed_executor_account(
    s: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    worker_id: uuid.UUID,
    executor_type: str,
) -> ModelAccount:
    account = ModelAccount(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        account_id=uuid.uuid4(),
        provider="executor",
        label="mac-mini",
        litellm_model=f"executor/{executor_type}",
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="unknown",
        is_active=True,
        extra_params={"worker_id": str(worker_id), "executor_type": executor_type},
    )
    s.add(account)
    await s.flush()
    return account


async def _open_run(s: AsyncSession, *, workspace_id: uuid.UUID, text: str) -> uuid.UUID:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.OPEN,
        payload={"intent_text": text},
    )
    s.add(run)
    await s.flush()
    return run.id


# --------------------------------------------------------------------------
# 1. KEYSTONE: executor run dispatches + verifies via the worker
# --------------------------------------------------------------------------


async def test_executor_run_dispatches_to_worker_and_verifies(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    executor_type = "claude_code"

    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=[executor_type])
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type=executor_type
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="ship the feature")
        await s.commit()

    # The real production factory must branch on provider == "executor" and
    # build an ExecutorOrchestrator (not the native RunOrchestrator).
    deps = build_agent_execution_deps(redis_client=redis)

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, ExecutorOrchestrator)

        # Simulate the worker: once a task is dispatched (status="dispatched"),
        # report success on the done channel + DB row — the worker's /result path.
        async def _simulate_worker() -> None:
            for _ in range(200):
                await asyncio.sleep(0.02)
                task = (
                    await s.execute(
                        select(dispatch.ExecutorTaskRow).where(
                            dispatch.ExecutorTaskRow.workspace_id == workspace_id,
                            dispatch.ExecutorTaskRow.status == "dispatched",
                        )
                    )
                ).scalar_one_or_none()
                if task is None:
                    continue
                await dispatch.record_result(
                    s,
                    task_id=task.id,
                    success=True,
                    output="implemented + tests green",
                    error_message=None,
                )
                await s.flush()
                await redis.publish(
                    dispatch.done_channel(task.id), json.dumps({"task_id": str(task.id)})
                )
                return

        runner = AgentRunner(s)
        worker_task = asyncio.create_task(_simulate_worker())
        result = await runner.drive(
            run_id=run_id, orchestrator=orchestrator, workspace_dir=tmp_path
        )
        await worker_task
        await s.commit()

    assert result.outcome == "verified"

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.REVIEW_READY

        deliverable = (await s.execute(select(Deliverable))).scalar_one()
        assert deliverable.run_id == run_id
        assert deliverable.deliverable_type is DeliverableType.CODE
        assert deliverable.payload.get("summary") == "implemented + tests green"

        deliver_event = (await s.execute(select(DeliveryEventRow))).scalar_one()
        assert deliver_event.deliverable_id == deliverable.id
        assert deliver_event.artifact_type == DeliverableType.CODE.value

        settle = (
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.run_id == run_id,
                        ExecutionRunActivity.activity_type == "settle",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(settle) == 1
        assert settle[0].payload.get("verified") is True

        task = (
            await s.execute(
                select(dispatch.ExecutorTaskRow).where(
                    dispatch.ExecutorTaskRow.workspace_id == workspace_id
                )
            )
        ).scalar_one()
        assert task.status == "done"
        assert task.executor_type == executor_type
        # The task prompt is framed from the run's intent text.
        assert "ship the feature" in task.prompt

    await redis.aclose()


# --------------------------------------------------------------------------
# 2. No worker available → Decision, run stays RUNNING (needs_decision)
# --------------------------------------------------------------------------


async def test_executor_run_no_worker_creates_decision(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()

    async with sf() as s:
        # Account exists but NO online worker carries the capability.
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=uuid.uuid4(), executor_type="claude_code"
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="do the thing")
        await s.commit()

    deps = build_agent_execution_deps(redis_client=redis)
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, ExecutorOrchestrator)
        runner = AgentRunner(s)
        result = await runner.drive(
            run_id=run_id, orchestrator=orchestrator, workspace_dir=tmp_path
        )
        await s.commit()

    assert result.outcome == "needs_decision"
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.RUNNING
        decisions = (await s.execute(select(Decision))).scalars().all()
        assert len(decisions) == 1
        assert decisions[0].run_id == run_id
        # No deliverable produced.
        assert (await s.execute(select(Deliverable))).first() is None

    await redis.aclose()


# --------------------------------------------------------------------------
# 3. Worker reports failure → system_error → run FAILED
# --------------------------------------------------------------------------


async def test_executor_run_worker_failure_fails_run(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    executor_type = "codex"

    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=[executor_type])
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type=executor_type
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="ship it")
        await s.commit()

    deps = build_agent_execution_deps(redis_client=redis)
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)

        async def _simulate_failing_worker() -> None:
            for _ in range(200):
                await asyncio.sleep(0.02)
                task = (
                    await s.execute(
                        select(dispatch.ExecutorTaskRow).where(
                            dispatch.ExecutorTaskRow.status == "dispatched"
                        )
                    )
                ).scalar_one_or_none()
                if task is None:
                    continue
                await dispatch.record_result(
                    s,
                    task_id=task.id,
                    success=False,
                    output="",
                    error_message="cli exited 1",
                )
                await s.flush()
                await redis.publish(
                    dispatch.done_channel(task.id), json.dumps({"task_id": str(task.id)})
                )
                return

        runner = AgentRunner(s)
        worker_task = asyncio.create_task(_simulate_failing_worker())
        result = await runner.drive(
            run_id=run_id, orchestrator=orchestrator, workspace_dir=tmp_path
        )
        await worker_task
        await s.commit()

    assert result.outcome == "system_error"
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.FAILED
        assert (await s.execute(select(Deliverable))).first() is None

    await redis.aclose()


# --------------------------------------------------------------------------
# 4. Non-executor (api-llm) account still builds the native RunOrchestrator
# --------------------------------------------------------------------------


async def test_non_executor_account_builds_native_orchestrator(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from backend.config import get_settings as _get_settings
    from backend.execution.orchestrator import RunOrchestrator
    from backend.gateway.llm_client import LlmClient
    from backend.workers import run as run_module

    # The native path eagerly builds the credential cipher (to decrypt the
    # account's api key) — provide a test KMS key so it constructs. It also
    # builds ``LlmClient()`` which lazily imports litellm (not a declared dep);
    # patch it to a no-op client so the smoke test exercises the *branch* (native
    # RunOrchestrator built, not ExecutorOrchestrator) without a real LLM dep.
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    _get_settings.cache_clear()
    monkeypatch.setattr(run_module, "LlmClient", lambda: LlmClient(completion_fn=lambda **_: None))

    workspace_id = uuid.uuid4()
    async with sf() as s:
        account = ModelAccount(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            account_id=uuid.uuid4(),
            provider="anthropic",
            label="claude",
            litellm_model="claude-3-5-sonnet",
            api_base=None,
            api_key_encrypted="ciphertext",
            data_jurisdiction="us",
            is_active=True,
            extra_params={},
        )
        s.add(account)
        run_id = await _open_run(s, workspace_id=workspace_id, text="native run")
        await s.commit()

    deps = build_agent_execution_deps()
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, RunOrchestrator)
        assert not isinstance(orchestrator, ExecutorOrchestrator)


# --------------------------------------------------------------------------
# 5. Executor account but no redis client → cannot dispatch → Decision
# --------------------------------------------------------------------------


async def test_executor_run_without_redis_creates_decision(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = uuid.uuid4()
    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=["claude_code"])
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type="claude_code"
        )
        run_id = await _open_run(s, workspace_id=workspace_id, text="no redis here")
        await s.commit()

    deps = build_agent_execution_deps()  # no redis_client
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        orchestrator = await deps.orchestrator_factory(s, run)
        assert isinstance(orchestrator, ExecutorOrchestrator)
        runner = AgentRunner(s)
        result = await runner.drive(
            run_id=run_id, orchestrator=orchestrator, workspace_dir=tmp_path
        )
        await s.commit()

    assert result.outcome == "needs_decision"
    async with sf() as s:
        decisions = (await s.execute(select(Decision))).scalars().all()
        assert len(decisions) == 1


# --------------------------------------------------------------------------
# 6. Timeout setting default sanity
# --------------------------------------------------------------------------


async def test_executor_task_timeout_setting_default() -> None:
    assert get_settings().executor_task_timeout_s == 1800.0
