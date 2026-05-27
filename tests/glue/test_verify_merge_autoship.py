"""W2 glue — verify-time merge + auto-ship end-to-end.

Drives a product-bound run through:

1. workspace_provisioner — git worktree from main
2. agent writes a file in the worktree
3. verify — backend commits the worktree + merges main in (clean)
4. AgentRunner.transition(REVIEW_READY) → auto-ship → SHIPPED

And a conflict case: another worktree shipped to main mid-flight, so
verify-time `merge main` finds a conflict, the worktree is left with
markers, verify outcome is FAILED with reason="merge_conflict" + paths.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.execution.db import (
    ExecutionBase,
    ExecutionRun,
    RunAttempt,
    RunStatus,
    VerificationOutcome,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.verifier.contract import VerificationContract
from backend.execution.verifier.service import VerificationService
from backend.orchestrator.agent_runner import AgentRunner
from backend.storage.product_workspace import (
    add_run_worktree,
    init_product_workspace,
    product_workspace_path,
    run_worktree_path,
)
from backend.supervisor.sandbox import NoopSandboxManager

from .._support import db_engine


class _StubJudgeLlm:
    """No-op judge LLM — the W2 merge tests use empty contracts so the
    judge step never runs, but VerificationService.__init__ requires
    one."""

    async def complete(self, *, messages, response_format):  # type: ignore[no-untyped-def]
        return {"passed": True, "reasoning": ""}


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _isolate_workspace_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(
        get_settings(), "product_workspace_root", str(tmp_path / "products"), raising=False
    )
    monkeypatch.setattr(get_settings(), "run_workspace_root", str(tmp_path / "runs"), raising=False)


async def _git(*args: str, cwd) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, f"git {args} failed: {err.decode()}"
    return out.decode().strip()


async def _seed_run_with_worktree(
    sf: async_sessionmaker[AsyncSession], *, intent: str = "build the answer"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, Path]:
    """Set up: product workspace init → worktree → ExecutionRun row +
    WorkStep + RunAttempt rows. Returns (run_id, work_step_id, product_id, worktree_path)."""
    product_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    await init_product_workspace(product_id)
    run_id = uuid.uuid4()
    worktree = await add_run_worktree(product_id, run_id)

    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                product_id=product_id,
                status=RunStatus.RUNNING,
                payload={"intent_text": intent},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        ws_id = uuid.uuid4()
        s.add(
            WorkStep(
                id=ws_id,
                run_id=run_id,
                workspace_id=workspace_id,
                title="step 1",
                status=WorkStepStatus.RUNNING,
                payload={},
            )
        )
        attempt_id = uuid.uuid4()
        s.add(
            RunAttempt(
                id=attempt_id,
                run_id=run_id,
                workspace_id=workspace_id,
                phase="verifying",
                payload={},
                started_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return run_id, ws_id, product_id, worktree


# ---------------------------------------------------------------------------
# Clean auto-ship
# ---------------------------------------------------------------------------


async def test_verified_run_auto_ships_to_main(sf: async_sessionmaker[AsyncSession]) -> None:
    """End-to-end: agent writes a file → verify (no command checks, clean
    merge) → AgentRunner transition to REVIEW_READY → auto-ship to SHIPPED.
    main now carries the file."""
    run_id, ws_id, product_id, worktree = await _seed_run_with_worktree(sf)

    # Simulate agent writing a file.
    (worktree / "hello.py").write_text("def add(a, b):\n    return a + b\n")

    # Verify with an empty contract (no commands, no judge) — the merge
    # step is what we're exercising. The W2 verify pre-step commits the
    # worktree + merges main in (clean), then command/judge run (empty).
    contract = VerificationContract(checks=())
    sandbox = NoopSandboxManager()

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        work_step = await s.get(WorkStep, ws_id)
        attempt = (
            await s.execute(select(RunAttempt).where(RunAttempt.run_id == run_id))
        ).scalar_one()

        box = await sandbox.acquire(product_id, str(worktree))
        verifier = VerificationService(session=s, llm=_StubJudgeLlm())
        vr = await verifier.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=[],
            final_text="done",
        )
        assert vr.outcome is VerificationOutcome.PASSED
        await s.commit()

    # AgentRunner.transition(REVIEW_READY) → auto-ship.
    async with sf() as s:
        runner = AgentRunner(s)
        await runner.transition(run_id=run_id, to_status=RunStatus.REVIEW_READY, reason="verified")
        await s.commit()

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run.status is RunStatus.SHIPPED

    # main has the agent's file + the worktree is cleaned up.
    product_path = product_workspace_path(product_id)
    assert (product_path / "hello.py").exists()
    assert (product_path / "hello.py").read_text() == "def add(a, b):\n    return a + b\n"
    # Worktree is gone (auto-ship cleanup ran).
    assert not run_worktree_path(run_id).exists()


# ---------------------------------------------------------------------------
# Conflict detected at verify time
# ---------------------------------------------------------------------------


async def test_verify_surfaces_merge_conflict_as_failed(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Main moves with a conflicting change → verify FAILS with reason
    "merge_conflict" + paths. The worktree is left in mid-merge state
    so the agent's NEXT round can read the markers and resolve."""
    run_id, ws_id, product_id, worktree = await _seed_run_with_worktree(sf)
    product_path = product_workspace_path(product_id)

    # Agent writes hello.py in worktree.
    (worktree / "hello.py").write_text("agent's add()\n")
    # Main commits a CONFLICTING hello.py directly.
    (product_path / "hello.py").write_text("MAIN'S version\n")
    await _git("add", "-A", cwd=product_path)
    await _git("commit", "-m", "main: conflict", cwd=product_path)

    contract = VerificationContract(checks=())
    sandbox = NoopSandboxManager()

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        work_step = await s.get(WorkStep, ws_id)
        attempt = (
            await s.execute(select(RunAttempt).where(RunAttempt.run_id == run_id))
        ).scalar_one()

        box = await sandbox.acquire(product_id, str(worktree))
        verifier = VerificationService(session=s, llm=_StubJudgeLlm())
        vr = await verifier.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=[],
            final_text="done",
        )
        await s.commit()

    assert vr.outcome is VerificationOutcome.FAILED
    assert vr.result["merge_conflict"] is True
    assert "hello.py" in vr.result["conflict_paths"]

    # Worktree has conflict markers — agent's next round will see them.
    content = (worktree / "hello.py").read_text()
    assert "<<<<<<<" in content


# ---------------------------------------------------------------------------
# Non-product run: verify skips merge step entirely
# ---------------------------------------------------------------------------


async def test_no_product_run_skips_merge_step(sf: async_sessionmaker[AsyncSession]) -> None:
    """A run without product_id (Direct path / legacy) goes through verify
    EXACTLY as before W2 — no merge attempt, no commit, no nothing."""
    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    ws_id = uuid.uuid4()
    attempt_id = uuid.uuid4()

    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                product_id=None,
                status=RunStatus.RUNNING,
                payload={"intent_text": "no product run"},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            WorkStep(
                id=ws_id,
                run_id=run_id,
                workspace_id=workspace_id,
                title="step 1",
                status=WorkStepStatus.RUNNING,
                payload={},
            )
        )
        s.add(
            RunAttempt(
                id=attempt_id,
                run_id=run_id,
                workspace_id=workspace_id,
                phase="verifying",
                payload={},
                started_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    contract = VerificationContract(checks=())
    sandbox = NoopSandboxManager()

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        work_step = await s.get(WorkStep, ws_id)
        attempt = await s.get(RunAttempt, attempt_id)

        # No product workspace, no worktree — just a fake path.
        box = await sandbox.acquire(uuid.uuid4(), "/tmp/nonexistent")
        verifier = VerificationService(session=s, llm=_StubJudgeLlm())
        vr = await verifier.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=[],
            final_text="done",
        )
        await s.commit()

    # Verify still passed (empty contract, no merge attempted).
    assert vr.outcome is VerificationOutcome.PASSED
    # No merge_conflict marker on the result.
    assert "merge_conflict" not in vr.result
