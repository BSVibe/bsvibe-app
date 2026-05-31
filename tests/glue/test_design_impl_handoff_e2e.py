"""Design→Impl handoff e2e — codex DESIGNS, opencode IMPLEMENTS.

The original product intent: a ``design_then_impl`` request runs a DESIGN stage
on one executor that writes a spec, then chains an IMPLEMENTATION run — seeded
with that spec — on a DIFFERENT executor. This drives the real two-CLI split:

* DESIGN stage → routed (by ``stage=design`` rule) to a **codex** executor → a
  live ``codex exec`` writes ``docs/spec.md``.
* The verified design run spawns an impl run (P1-L2 handoff) carrying
  ``design_artifact_refs``.
* IMPL stage → routed (by ``stage=impl`` rule) to an **opencode** executor →
  ``read_design_context`` folds the spec into the prompt and a live
  ``opencode run`` implements it.

Routing (``resolve_route``) is REAL — it picks codex for design and opencode for
impl off the seeded ``run_routing_rules``. The verification seams are doubles
(the WORK is real). Gated + costly: runs only with ``BSVIBE_E2E_CODEX=1`` AND
both ``codex`` + ``opencode`` on PATH (real CLI calls). In-memory SQLite +
fakeredis.

    BSVIBE_E2E_CODEX=1 uv run pytest tests/glue/test_design_impl_handoff_e2e.py -v -s
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.executors.db  # noqa: F401 — register tables on the shared Base
from backend.config import get_settings
from backend.execution.db import Deliverable, ExecutionRun, RunStatus
from backend.execution.handoff import read_design_context
from backend.executors import dispatch
from backend.executors.orchestrator import ExecutorOrchestrator
from backend.executors.worker.codex import CodexExecutor
from backend.executors.worker.main import _collect_workspace_files
from backend.executors.worker.opencode import OpenCodeExecutor
from backend.orchestrator.agent_runner import AgentRunner
from backend.router.routing.run_routing.db import RunRoutingRuleRow
from backend.router.routing.run_routing.engine import resolve_route

from .._support import db_engine
from .test_executor_run_e2e import (
    _FakeBox,
    _FakeSandboxManager,
    _make_redis,
    _seed_executor_account,
    _seed_worker,
    _StubJudge,
    _StubRetriever,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("BSVIBE_E2E_CODEX") != "1"
        or shutil.which("codex") is None
        or shutil.which("opencode") is None,
        reason="real design→impl e2e — set BSVIBE_E2E_CODEX=1 with `codex` + `opencode` on PATH",
    ),
]

_OPENCODE_MODEL = os.environ.get("BSVIBE_E2E_OPENCODE_MODEL") or None


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _claim_dispatched_task(
    sf: async_sessionmaker[AsyncSession], *, worker_id: uuid.UUID
) -> dispatch.ExecutorTaskRow:
    """Poll for THIS worker's currently-``dispatched`` (not-yet-terminal) task.

    The two stages share one worker stream, so replaying the stream from ``0``
    would re-pick the prior (terminal) design task. The orchestrator commits the
    task as ``dispatched`` before awaiting, so a DB poll for the live dispatched
    row picks the right task per stage (the prior one is already ``done``)."""
    for _ in range(600):
        async with sf() as s:
            row = (
                (
                    await s.execute(
                        select(dispatch.ExecutorTaskRow)
                        .where(
                            dispatch.ExecutorTaskRow.worker_id == worker_id,
                            dispatch.ExecutorTaskRow.status == "dispatched",
                        )
                        .order_by(dispatch.ExecutorTaskRow.created_at.desc())
                    )
                )
                .scalars()
                .first()
            )
            if row is not None:
                return row
        await asyncio.sleep(0.1)
    raise AssertionError(f"no dispatched task for worker {worker_id}")


async def _run_real_executor_worker(
    redis: Any,
    *,
    worker_id: uuid.UUID,
    sf: async_sessionmaker[AsyncSession],
    run_workspace_root: str,
) -> dict[str, Any]:
    """Real worker: claim the dispatched task, pick the matching CLI executor by
    ``executor_type`` (codex / opencode), run it in a fresh workspace, capture
    the produced files (B1), and report the result — the worker's real contract."""
    task = await _claim_dispatched_task(sf, worker_id=worker_id)
    task_id = task.id
    prompt, system, executor_type = task.prompt, task.system, task.executor_type

    if executor_type == "codex":
        executor: Any = CodexExecutor(timeout_seconds=240)
        model = None
    elif executor_type == "opencode":
        executor = OpenCodeExecutor(timeout_seconds=240)
        model = _OPENCODE_MODEL
    else:  # pragma: no cover — only codex/opencode are seeded
        raise AssertionError(f"unexpected executor_type {executor_type}")

    work_dir = tempfile.mkdtemp(prefix=f"{executor_type}-e2e-")
    output_parts: list[str] = []
    error: str | None = None
    try:
        async for chunk in executor.execute(
            prompt, {"workspace_dir": work_dir, "system": system, "model": model}
        ):
            if chunk.delta:
                output_parts.append(chunk.delta)
            if chunk.done and chunk.error:
                error = chunk.error
        files = _collect_workspace_files(work_dir)
        async with sf() as worker_s:
            await dispatch.record_result(
                worker_s,
                redis,
                task_id=task_id,
                success=error is None,
                output="".join(output_parts),
                error_message=error,
                files=files,
                run_workspace_root=run_workspace_root,
            )
            await worker_s.commit()
        return {
            "executor_type": executor_type,
            "error": error,
            "files": {f["path"] for f in files},
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def _seed_stage_rule(
    s: AsyncSession, *, workspace_id: uuid.UUID, stage: str, target: str, priority: int
) -> None:
    s.add(
        RunRoutingRuleRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            name=f"{stage}-stage",
            priority=priority,
            is_default=False,
            target=target,
            conditions=[{"field": "stage", "operator": "eq", "value": stage}],
            is_active=True,
        )
    )
    await s.flush()


def _orchestrator(session, *, redis, account, settings):
    """An ExecutorOrchestrator with verification doubles (the WORK is real)."""
    return ExecutorOrchestrator(
        session=session,
        redis=redis,
        account=account,
        settings=settings,
        sandbox_manager=_FakeSandboxManager(_FakeBox(files={})),
        retriever=_StubRetriever(["the artifact satisfies the spec"]),
        verify_llm=_StubJudge(passed=True),
    )


async def test_codex_designs_then_opencode_implements(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    run_workspace_root = str(tmp_path / "runs")
    settings = get_settings().model_copy(update={"executor_task_timeout_s": 300.0})

    # Seed: one worker with both capabilities, a codex + an opencode executor
    # account, and stage→executor routing rules (the chaining gate + the split).
    async with sf() as s:
        worker = await _seed_worker(
            s, workspace_id=workspace_id, capabilities=["codex", "opencode"]
        )
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type="codex"
        )
        await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type="opencode"
        )
        await _seed_stage_rule(
            s, workspace_id=workspace_id, stage="design", target="executor/codex", priority=10
        )
        await _seed_stage_rule(
            s, workspace_id=workspace_id, stage="impl", target="executor/opencode", priority=20
        )
        design_run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=None,
            request_id=uuid.uuid4(),
            status=RunStatus.OPEN,
            payload={
                "intent_text": (
                    "Write a brief design spec to docs/spec.md for a Python function "
                    "add(a, b) that returns a + b. Create only docs/spec.md."
                ),
                "frame": {"artifact_type_hint": "code", "pipeline": "design_then_impl"},
            },
        )
        s.add(design_run)
        await s.commit()
        design_run_id = design_run.id

    # --- DESIGN stage: routes to codex, real codex writes the spec ----------
    async with sf() as s:
        design = await s.get(ExecutionRun, design_run_id)
        design_account = await resolve_route(s, design)
        assert design_account is not None
        assert design_account.litellm_model == "executor/codex", "design must route to codex"

        runner = AgentRunner(s)
        drive = asyncio.create_task(
            runner.drive(
                run_id=design_run_id,
                orchestrator=_orchestrator(
                    s, redis=redis, account=design_account, settings=settings
                ),
                workspace_dir=tmp_path,
            )
        )
        worker_t = asyncio.create_task(
            _run_real_executor_worker(
                redis, worker_id=worker.id, sf=sf, run_workspace_root=run_workspace_root
            )
        )
        design_result = await drive
        design_worker = await worker_t
        await s.commit()

    assert design_worker["executor_type"] == "codex"
    assert design_worker["error"] is None, design_worker["error"]
    assert "docs/spec.md" in design_worker["files"], design_worker["files"]
    assert design_result.outcome == "verified", design_result

    # --- HANDOFF: the verified design run spawned an impl run --------------
    async with sf() as s:
        impl_rows = [
            r
            for r in (await s.execute(select(ExecutionRun))).scalars().all()
            if r.id != design_run_id and (r.payload or {}).get("stage") == "impl"
        ]
        assert len(impl_rows) == 1, "design run must spawn exactly one impl run"
        impl = impl_rows[0]
        assert impl.status is RunStatus.OPEN
        assert impl.payload["design_run_id"] == str(design_run_id)
        assert "docs/spec.md" in impl.payload["design_artifact_refs"]

        # The design spec is readable for the impl prompt (the handoff seed).
        design_context = read_design_context(impl, settings)
        assert design_context is not None and "add" in design_context.lower(), design_context

        impl_account = await resolve_route(s, impl)
        assert impl_account is not None
        assert impl_account.litellm_model == "executor/opencode", "impl must route to opencode"
        impl_run_id = impl.id

    # --- IMPL stage: routes to opencode, real opencode implements ----------
    async with sf() as s:
        impl = await s.get(ExecutionRun, impl_run_id)
        impl_account = await resolve_route(s, impl)
        runner = AgentRunner(s)
        drive = asyncio.create_task(
            runner.drive(
                run_id=impl_run_id,
                orchestrator=_orchestrator(s, redis=redis, account=impl_account, settings=settings),
                workspace_dir=tmp_path,
            )
        )
        worker_t = asyncio.create_task(
            _run_real_executor_worker(
                redis, worker_id=worker.id, sf=sf, run_workspace_root=run_workspace_root
            )
        )
        impl_result = await drive
        impl_worker = await worker_t
        await s.commit()

    assert impl_worker["executor_type"] == "opencode"
    assert impl_worker["error"] is None, impl_worker["error"]
    # opencode implemented something (produced at least one file).
    assert impl_worker["files"], "opencode produced no files"
    assert impl_result.outcome == "verified", impl_result

    async with sf() as s:
        impl = await s.get(ExecutionRun, impl_run_id)
        assert impl is not None and impl.status is RunStatus.REVIEW_READY
        # Both stages produced verified deliverables (codex's spec + opencode's code).
        deliverables = (await s.execute(select(Deliverable))).scalars().all()
        runs_with_deliverables = {d.run_id for d in deliverables}
        assert design_run_id in runs_with_deliverables
        assert impl_run_id in runs_with_deliverables

    await redis.aclose()
