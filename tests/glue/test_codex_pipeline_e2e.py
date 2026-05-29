"""Real codex executor — full agent-run pipeline with a live `codex exec`.

The keystone executor test (``test_executor_run_e2e``) SIMULATES the worker with
``dispatch.record_result``. This one runs the **real** ``codex exec --json`` CLI
as the worker: a ``provider='executor'`` run dispatches a task onto the worker
stream, a real codex subprocess produces a file in a fresh workspace, the worker
captures it (B1) + reports done, and the ExecutorOrchestrator verifies + lands a
REVIEW_READY Deliverable carrying codex's REAL artifact.

This is **gated + costly**: it invokes the real codex CLI (real OpenAI calls), so
it runs only when ``BSVIBE_E2E_CODEX=1`` AND ``codex`` is on PATH. Skipped
otherwise — CI + the default suite never pay for it. Runs on in-memory SQLite +
fakeredis (no Postgres needed — the executor pipeline isn't pgvector).

    BSVIBE_E2E_CODEX=1 uv run pytest tests/glue/test_codex_pipeline_e2e.py -v -s
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
from backend.execution.db import Deliverable, DeliverableType, ExecutionRun, RunStatus
from backend.executors import dispatch
from backend.executors.orchestrator import ExecutorOrchestrator
from backend.executors.worker.codex import CodexExecutor
from backend.executors.worker.main import _collect_workspace_files
from backend.orchestrator.agent_runner import AgentRunner

from .._support import db_engine

# Reuse the keystone harness's seed + double helpers verbatim.
from .test_executor_run_e2e import (
    _await_dispatched_task_id,
    _FakeBox,
    _FakeSandboxManager,
    _make_redis,
    _open_run,
    _seed_executor_account,
    _seed_worker,
    _StubJudge,
    _StubRetriever,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("BSVIBE_E2E_CODEX") != "1" or shutil.which("codex") is None,
        reason="real codex e2e — set BSVIBE_E2E_CODEX=1 and have `codex` on PATH (real OpenAI cost)",
    ),
]


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _run_real_codex_worker(
    redis: Any,
    *,
    worker_id: uuid.UUID,
    sf: async_sessionmaker[AsyncSession],
    run_workspace_root: str,
) -> dict[str, Any]:
    """The REAL worker: learn the dispatched task from the stream, run
    ``codex exec`` in a fresh workspace, capture the produced files (B1), and
    report the result on a separate session — exactly the worker's contract."""
    task_id = await _await_dispatched_task_id(redis, worker_id=worker_id)
    async with sf() as s:
        task = await s.get(dispatch.ExecutorTaskRow, task_id)
        assert task is not None
        prompt, system = task.prompt, task.system

    work_dir = tempfile.mkdtemp(prefix="codex-e2e-")
    output_parts: list[str] = []
    error: str | None = None
    try:
        executor = CodexExecutor(timeout_seconds=240)
        async for chunk in executor.execute(
            prompt, {"workspace_dir": work_dir, "system": system, "model": None}
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
        return {"output": "".join(output_parts), "error": error, "files": files}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def test_codex_produces_verified_deliverable_through_pipeline(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    run_workspace_root = str(tmp_path / "runs")

    async with sf() as s:
        worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=["codex"])
        account = await _seed_executor_account(
            s, workspace_id=workspace_id, worker_id=worker.id, executor_type="codex"
        )
        run_id = await _open_run(
            s,
            workspace_id=workspace_id,
            text=(
                "Create a single file named fib.py containing a function "
                "fib(n) that returns the nth Fibonacci number (0-indexed, "
                "fib(0)=0, fib(1)=1). Do not create any other files."
            ),
        )
        await s.commit()

    # Real codex needs minutes, not the 30s keystone cap — give await_completion room.
    settings = get_settings().model_copy(update={"executor_task_timeout_s": 300.0})

    async with sf() as orch_s:
        run = await orch_s.get(ExecutionRun, run_id)
        assert run is not None
        # Verification doubles (the judge/sandbox/canon seams prod wires) so the
        # PASS path is exercised; the WORK itself is real codex.
        manager = _FakeSandboxManager(_FakeBox(files={}))
        orchestrator = ExecutorOrchestrator(
            session=orch_s,
            redis=redis,
            account=account,
            settings=settings,
            sandbox_manager=manager,
            retriever=_StubRetriever(["fib.py defines a correct fib(n)"]),
            verify_llm=_StubJudge(passed=True),
        )
        runner = AgentRunner(orch_s)
        drive_task = asyncio.create_task(
            runner.drive(run_id=run_id, orchestrator=orchestrator, workspace_dir=tmp_path)
        )
        worker_task = asyncio.create_task(
            _run_real_codex_worker(
                redis, worker_id=worker.id, sf=sf, run_workspace_root=run_workspace_root
            )
        )
        result = await drive_task
        worker_result = await worker_task
        await orch_s.commit()

    # The real codex run must have produced fib.py.
    assert worker_result["error"] is None, worker_result["error"]
    produced = {f["path"] for f in worker_result["files"]}
    assert "fib.py" in produced, f"codex did not write fib.py — produced {produced}"

    assert result.outcome == "verified", result

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.REVIEW_READY

        deliverable = (await s.execute(select(Deliverable))).scalar_one()
        assert deliverable.deliverable_type is DeliverableType.CODE
        # The verified Deliverable carries codex's REAL captured artifact.
        refs = deliverable.payload.get("artifact_refs") or []
        assert "fib.py" in refs, refs

        task = (
            await s.execute(
                select(dispatch.ExecutorTaskRow).where(
                    dispatch.ExecutorTaskRow.workspace_id == workspace_id
                )
            )
        ).scalar_one()
        assert task.status == "done"
        assert "fib.py" in (task.artifact_refs or [])

    # The persisted artifact is real codex-authored Python.
    persisted = Path(run_workspace_root) / str(run_id) / "fib.py"
    assert persisted.is_file(), f"captured artifact not persisted at {persisted}"
    source = persisted.read_text(encoding="utf-8")
    assert "def fib" in source, source

    await redis.aclose()
