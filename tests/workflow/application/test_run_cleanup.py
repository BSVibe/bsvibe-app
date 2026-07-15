"""run_cleanup — cancel / discard a run + cascade-cancel a product's runs.

Fixes the orphaned-run bug: deleting a product left its ExecutionRuns behind
(product_id is a loose reference, no FK cascade), and there was no path to clear
a ``review_ready`` run that had no Safe Mode entry. These service functions are
the canonical primitives the MCP tools + product-delete cascade both call.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.application.run_cleanup import (
    cancel_product_runs,
    cancel_run,
    discard_run,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)

from ..._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


async def _seed_run(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    status: RunStatus,
    product_id: uuid.UUID | None = None,
) -> uuid.UUID:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=product_id,
        status=status,
        payload={"text": "build the thing"},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.add(run)
    await session.flush()
    return run.id


async def _seed_deliverable(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    handles: list[dict] | None = None,
) -> uuid.UUID:
    d = Deliverable(
        id=uuid.uuid4(),
        run_id=run_id,
        workspace_id=workspace_id,
        deliverable_type=DeliverableType.DIRECT_OUTPUT,
        payload={},
        compensation_handles=handles,
        created_at=datetime.now(tz=UTC),
    )
    session.add(d)
    await session.flush()
    return d.id


async def _seed_decision(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    status: DecisionStatus = DecisionStatus.PENDING,
) -> uuid.UUID:
    d = Decision(
        id=uuid.uuid4(),
        run_id=run_id,
        workspace_id=workspace_id,
        decision="verify",
        status=status,
        payload={},
        created_at=datetime.now(tz=UTC),
    )
    session.add(d)
    await session.flush()
    return d.id


# --- cancel_run (OPEN/RUNNING only, mirrors REST /cancel) ------------------


@pytest.mark.parametrize("status", [RunStatus.OPEN, RunStatus.RUNNING])
async def test_cancel_run_cancels_inflight(sf, workspace_id, status) -> None:
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=status)
        outcome = await cancel_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
        await s.commit()
    assert outcome.found is True
    assert outcome.cancelled is True
    assert outcome.status == "cancelled"


async def test_cancel_run_review_ready_not_cancellable(sf, workspace_id) -> None:
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.REVIEW_READY)
        outcome = await cancel_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
    assert outcome.found is True
    assert outcome.cancelled is False
    assert outcome.status == "review_ready"


async def test_cancel_run_cross_workspace_not_found(sf, workspace_id) -> None:
    async with sf() as s:
        run_id = await _seed_run(s, uuid.uuid4(), status=RunStatus.RUNNING)
        outcome = await cancel_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
    assert outcome.found is False


# --- discard_run (any non-terminal → cancelled + best-effort tombstone) ----


async def test_discard_cancels_review_ready_and_tombstones_handleless(sf, workspace_id) -> None:
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.REVIEW_READY)
        d_id = await _seed_deliverable(s, workspace_id, run_id, handles=None)
        outcome = await discard_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
        await s.commit()

    assert outcome is not None
    assert outcome.cancelled is True
    assert outcome.status == "cancelled"
    assert str(d_id) in outcome.deliverables_retracted
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        deliv = await s.get(Deliverable, d_id)
        assert run.status is RunStatus.CANCELLED
        assert deliv.retracted_at is not None


async def test_discard_surfaces_deliverables_with_compensation_handles(sf, workspace_id) -> None:
    """A deliverable with captured compensation handles is NOT silently tombstoned
    (that would falsely claim its external artifact was rolled back) — it's
    surfaced for an explicit compensating retract instead."""
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.REVIEW_READY)
        d_id = await _seed_deliverable(
            s,
            workspace_id,
            run_id,
            handles=[{"plugin": "github", "artifact_type": "pr", "handle": {"n": 1}}],
        )
        outcome = await discard_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
        await s.commit()

    assert outcome.cancelled is True
    assert str(d_id) in outcome.deliverables_need_compensation
    assert str(d_id) not in outcome.deliverables_retracted
    async with sf() as s:
        deliv = await s.get(Deliverable, d_id)
        assert deliv.retracted_at is None  # not faked


async def test_discard_resolves_pending_decisions(sf, workspace_id) -> None:
    """Discard must resolve the run's PENDING decisions — the Summary dashboard
    lists pending Decisions, so cancelling the run alone leaves the card up."""
    actor = uuid.uuid4()
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.REVIEW_READY)
        dec_id = await _seed_decision(s, workspace_id, run_id)
        outcome = await discard_run(
            s, run_id=run_id, workspace_id=workspace_id, reason="mcp", actor_id=actor
        )
        await s.commit()

    assert str(dec_id) in outcome.decisions_resolved
    async with sf() as s:
        dec = await s.get(Decision, dec_id)
        assert dec.status is DecisionStatus.RESOLVED
        assert dec.resolved_at is not None
        assert dec.resolved_by == actor


async def test_discard_already_resolved_decision_untouched(sf, workspace_id) -> None:
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.REVIEW_READY)
        dec_id = await _seed_decision(s, workspace_id, run_id, status=DecisionStatus.RESOLVED)
        outcome = await discard_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
        await s.commit()
    assert str(dec_id) not in outcome.decisions_resolved


async def test_cancel_product_runs_resolves_pending_decisions(sf, workspace_id) -> None:
    product_id = uuid.uuid4()
    async with sf() as s:
        run_id = await _seed_run(
            s, workspace_id, status=RunStatus.REVIEW_READY, product_id=product_id
        )
        dec_id = await _seed_decision(s, workspace_id, run_id)
        await cancel_product_runs(
            s, product_id=product_id, workspace_id=workspace_id, reason="product deleted"
        )
        await s.commit()
    async with sf() as s:
        assert (await s.get(Decision, dec_id)).status is DecisionStatus.RESOLVED


async def test_discard_unknown_returns_none(sf, workspace_id) -> None:
    async with sf() as s:
        outcome = await discard_run(s, run_id=uuid.uuid4(), workspace_id=workspace_id, reason="mcp")
    assert outcome is None


# --- cancel aborts a mid-merge worktree (B4 — no lingering markers) ---------


async def _git_ok(*args: str, cwd) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(cwd)
    )
    _, err = await proc.communicate()
    assert proc.returncode == 0, f"git {args} failed: {err.decode()}"


async def _merge_in_progress(worktree: Path) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "-q",
        "--verify",
        "MERGE_HEAD",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(worktree),
    )
    await proc.communicate()
    return proc.returncode == 0


async def test_cancel_aborts_mid_merge_worktree(sf, workspace_id, tmp_path, monkeypatch) -> None:
    """A run cancelled while a verify-time ``merge main`` is mid-flight must not
    leave conflict markers behind — ``cancel_run`` aborts the merge. (Unlike
    ``discard``, ``cancel`` keeps the worktree on disk, so the abort is the only
    thing that cleans it.)"""
    from backend.config import get_settings
    from backend.storage.product_workspace import (
        add_run_worktree,
        commit_worktree,
        init_product_workspace,
        merge_main_into_worktree,
        product_workspace_path,
        run_worktree_path,
    )

    monkeypatch.setattr(
        get_settings(), "product_workspace_root", str(tmp_path / "products"), raising=False
    )
    monkeypatch.setattr(get_settings(), "run_workspace_root", str(tmp_path / "runs"), raising=False)

    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)

    # Agent commits, main commits a conflicting change, verify-time merge leaves
    # the worktree mid-merge with ``<<<<<<<`` markers.
    (worktree / "hello.py").write_text("agent\n")
    await commit_worktree(product_id, run_id, message="agent")
    product_path = product_workspace_path(product_id)
    (product_path / "hello.py").write_text("main\n")
    await _git_ok("add", "-A", cwd=product_path)
    await _git_ok("commit", "-m", "main: conflict", cwd=product_path)
    outcome_merge = await merge_main_into_worktree(product_id, run_id)
    assert outcome_merge.status == "conflict"
    assert await _merge_in_progress(worktree)  # precondition: poisoned tree

    # A RUNNING run is cancelled — the worktree stays, but the merge is aborted.
    async with sf() as s:
        run = ExecutionRun(
            id=run_id,
            workspace_id=workspace_id,
            product_id=product_id,
            status=RunStatus.RUNNING,
            payload={"text": "build"},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.flush()
        outcome = await cancel_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
        await s.commit()

    assert outcome.cancelled is True
    # Worktree still exists (cancel does not remove it) but is no longer mid-merge.
    assert run_worktree_path(run_id).exists()
    assert not await _merge_in_progress(worktree)
    assert "<<<<<<<" not in (worktree / "hello.py").read_text()


# --- cascade cancel on product delete --------------------------------------


async def test_cancel_product_runs_cancels_non_terminal_only(sf, workspace_id) -> None:
    product_id = uuid.uuid4()
    async with sf() as s:
        open_id = await _seed_run(s, workspace_id, status=RunStatus.OPEN, product_id=product_id)
        rr_id = await _seed_run(
            s, workspace_id, status=RunStatus.REVIEW_READY, product_id=product_id
        )
        shipped_id = await _seed_run(
            s, workspace_id, status=RunStatus.SHIPPED, product_id=product_id
        )
        # A run for a DIFFERENT product must be untouched.
        other_id = await _seed_run(s, workspace_id, status=RunStatus.OPEN, product_id=uuid.uuid4())
        n = await cancel_product_runs(
            s, product_id=product_id, workspace_id=workspace_id, reason="product deleted"
        )
        await s.commit()

    assert n == 2  # open + review_ready
    async with sf() as s:
        assert (await s.get(ExecutionRun, open_id)).status is RunStatus.CANCELLED
        assert (await s.get(ExecutionRun, rr_id)).status is RunStatus.CANCELLED
        assert (await s.get(ExecutionRun, shipped_id)).status is RunStatus.SHIPPED
        assert (await s.get(ExecutionRun, other_id)).status is RunStatus.OPEN
