"""run_cleanup — cancel / discard a run + cascade-cancel a product's runs.

Fixes the orphaned-run bug: deleting a product left its ExecutionRuns behind
(product_id is a loose reference, no FK cascade), and there was no path to clear
a ``review_ready`` run that had no Safe Mode entry. These service functions are
the canonical primitives the MCP tools + product-delete cascade both call.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.workflow.application.run_cleanup import (
    cancel_product_runs,
    cancel_run,
    discard_run,
    reap_orphan_product_workspaces,
    reap_terminal_run_workspaces,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.delivery.db import (
    SafeModeQueueItemRow,
    SafeModeStatus,
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


async def _seed_safe_mode_item(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    status: SafeModeStatus = SafeModeStatus.PENDING,
) -> uuid.UUID:
    item = SafeModeQueueItemRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        run_id=run_id,
        deliverable_id=uuid.uuid4(),
        status=status,
        expires_at=datetime.now(tz=UTC),
        created_at=datetime.now(tz=UTC),
    )
    session.add(item)
    await session.flush()
    return item.id


# --- cancel_run (OPEN/RUNNING only, mirrors REST /cancel) ------------------


async def test_cancel_run_denies_pending_safe_mode_items(sf, workspace_id) -> None:
    """Cancel must also deny the run's PENDING safe-mode approval items — a
    cancelled run's deliverables never deliver, so their approval cards must drop
    off the queue (orphaned-half, parallel to pending decisions)."""
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.RUNNING)
        item_id = await _seed_safe_mode_item(s, workspace_id, run_id)
        outcome = await cancel_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
        await s.commit()
    assert outcome.cancelled is True
    assert str(item_id) in outcome.safe_mode_items_resolved
    async with sf() as s:
        item = await s.get(SafeModeQueueItemRow, item_id)
        assert item is not None and item.status is SafeModeStatus.DENIED
        assert item.decided_at is not None


async def test_cancel_run_not_cancellable_leaves_item_pending(sf, workspace_id) -> None:
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.REVIEW_READY)
        item_id = await _seed_safe_mode_item(s, workspace_id, run_id)
        outcome = await cancel_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
        await s.commit()
    assert outcome.cancelled is False
    assert outcome.safe_mode_items_resolved == []
    async with sf() as s:
        assert (await s.get(SafeModeQueueItemRow, item_id)).status is SafeModeStatus.PENDING


async def test_discard_denies_pending_safe_mode_items(sf, workspace_id) -> None:
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.REVIEW_READY)
        item_id = await _seed_safe_mode_item(s, workspace_id, run_id)
        outcome = await discard_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
        await s.commit()
    assert str(item_id) in outcome.safe_mode_items_resolved
    async with sf() as s:
        assert (await s.get(SafeModeQueueItemRow, item_id)).status is SafeModeStatus.DENIED


async def test_cancel_product_runs_denies_pending_safe_mode_items(sf, workspace_id) -> None:
    product_id = uuid.uuid4()
    async with sf() as s:
        run_id = await _seed_run(
            s, workspace_id, status=RunStatus.REVIEW_READY, product_id=product_id
        )
        item_id = await _seed_safe_mode_item(s, workspace_id, run_id)
        await cancel_product_runs(
            s, product_id=product_id, workspace_id=workspace_id, reason="product deleted"
        )
        await s.commit()
    async with sf() as s:
        assert (await s.get(SafeModeQueueItemRow, item_id)).status is SafeModeStatus.DENIED


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


async def test_cancel_run_resolves_pending_decisions(sf, workspace_id) -> None:
    """Cancel must resolve the run's PENDING decisions too — else the Summary
    "확인 필요" card lingers forever after the run is cancelled (orphaned-half).
    Mirrors discard_run; previously only discard/cancel_product_runs did this."""
    actor = uuid.uuid4()
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.RUNNING)
        dec_id = await _seed_decision(s, workspace_id, run_id)
        outcome = await cancel_run(
            s, run_id=run_id, workspace_id=workspace_id, reason="mcp", actor_id=actor
        )
        await s.commit()

    assert outcome.cancelled is True
    assert str(dec_id) in outcome.decisions_resolved
    async with sf() as s:
        dec = await s.get(Decision, dec_id)
        assert dec.status is DecisionStatus.RESOLVED
        assert dec.resolved_at is not None
        assert dec.resolved_by == actor


async def test_cancel_run_not_cancellable_leaves_decision_pending(sf, workspace_id) -> None:
    """A non-in-flight run (review_ready) is not cancelled by cancel_run, so its
    pending decision must be left untouched (no false resolution)."""
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.REVIEW_READY)
        dec_id = await _seed_decision(s, workspace_id, run_id)
        outcome = await cancel_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
        await s.commit()
    assert outcome.cancelled is False
    assert outcome.decisions_resolved == []
    async with sf() as s:
        assert (await s.get(Decision, dec_id)).status is DecisionStatus.PENDING


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


# ---------------------------------------------------------------------------
# reap_terminal_run_workspaces — the periodic disk-bounding sweep
# ---------------------------------------------------------------------------


async def test_reap_terminal_run_workspaces_removes_only_terminal(sf, workspace_id, tmp_path):
    """The sweep reclaims the on-disk workspace of every TERMINAL run
    (shipped / failed / cancelled) and LEAVES ALONE non-terminal runs (in use),
    dirs with no matching run row (a brand-new run mid-clone), and non-UUID
    dirs. This is the backstop that bounds var/runs — the FAILED path has no
    inline cleanup hook and github clones leak on every path."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    product_id = uuid.uuid4()

    async with sf() as s:
        shipped = await _seed_run(s, workspace_id, status=RunStatus.SHIPPED, product_id=product_id)
        failed = await _seed_run(s, workspace_id, status=RunStatus.FAILED)
        cancelled = await _seed_run(s, workspace_id, status=RunStatus.CANCELLED)
        running = await _seed_run(s, workspace_id, status=RunStatus.RUNNING)
        open_ = await _seed_run(s, workspace_id, status=RunStatus.OPEN)
        await s.commit()

    unknown = uuid.uuid4()  # a dir with no run row (e.g. mid-clone brand-new run)
    for rid in (shipped, failed, cancelled, running, open_, unknown):
        (runs_root / str(rid)).mkdir()
    (runs_root / "not-a-uuid").mkdir()  # never a run workspace

    removed: list[tuple] = []

    async def fake_remover(pid, rid):
        removed.append((pid, rid))

    async with sf() as s:
        reaped = await reap_terminal_run_workspaces(s, remover=fake_remover, runs_root=runs_root)

    assert set(reaped) == {shipped, failed, cancelled}
    assert {rid for _pid, rid in removed} == {shipped, failed, cancelled}
    # product_id is threaded through for the git-worktree prune on local products.
    assert (product_id, shipped) in removed
    # Non-terminal + unknown + non-uuid are never handed to the remover.
    assert running not in {rid for _pid, rid in removed}
    assert open_ not in {rid for _pid, rid in removed}
    assert unknown not in {rid for _pid, rid in removed}


async def test_reap_terminal_run_workspaces_no_runs_dir_is_noop(sf, tmp_path):
    """A missing runs root (fresh box / not-yet-created) is a no-op, not an
    error."""
    async with sf() as s:
        reaped = await reap_terminal_run_workspaces(
            s, remover=_unreachable_remover, runs_root=tmp_path / "does-not-exist"
        )
    assert reaped == []


async def test_reap_terminal_run_workspaces_continues_on_remover_error(sf, workspace_id, tmp_path):
    """One workspace failing to remove must not abort the sweep — the rest are
    still reclaimed and the worker retries the failed one next pass."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()

    async with sf() as s:
        bad = await _seed_run(s, workspace_id, status=RunStatus.SHIPPED)
        good = await _seed_run(s, workspace_id, status=RunStatus.FAILED)
        await s.commit()
    for rid in (bad, good):
        (runs_root / str(rid)).mkdir()

    async def flaky_remover(pid, rid):
        if rid == bad:
            raise OSError("device busy")

    async with sf() as s:
        reaped = await reap_terminal_run_workspaces(s, remover=flaky_remover, runs_root=runs_root)

    assert reaped == [good]  # the failed one is not reported reaped, no raise


async def _unreachable_remover(pid, rid):  # pragma: no cover
    raise AssertionError("remover must not be called when there is nothing to reap")


async def test_reap_removes_orphan_dirs_older_than_grace(sf, workspace_id, tmp_path):
    """A dir whose run row NO LONGER EXISTS (the run was hard-deleted with its
    product, or purged) is never matched by the terminal query — 49 such orphans
    (760MB) sat in production forever. They ARE reclaimable, but only past a
    grace period: a brand-new run mid-clone also has no committed row yet, and
    reaping that would destroy live work. Old orphan → reaped; fresh orphan →
    kept."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()

    async with sf() as s:
        live = await _seed_run(s, workspace_id, status=RunStatus.RUNNING)
        await s.commit()

    old_orphan = uuid.uuid4()
    fresh_orphan = uuid.uuid4()
    for rid in (old_orphan, fresh_orphan, live):
        (runs_root / str(rid)).mkdir()
    # Age the orphan past the grace window (mtime 48h ago).
    old_ts = datetime.now(tz=UTC).timestamp() - 48 * 3600
    os.utime(runs_root / str(old_orphan), (old_ts, old_ts))

    removed: list[uuid.UUID] = []

    async def fake_remover(pid, rid):
        removed.append(rid)

    async with sf() as s:
        reaped = await reap_terminal_run_workspaces(
            s, remover=fake_remover, runs_root=runs_root, orphan_grace_s=24 * 3600
        )

    assert old_orphan in reaped, "an aged orphan must be reclaimed"
    assert fresh_orphan not in reaped, "a fresh dir may be a run mid-clone — keep"
    assert live not in reaped, "a live run's workspace must never be touched"
    assert removed == [old_orphan]


async def test_reap_orphan_passes_none_product_id(sf, tmp_path):
    """An orphan has no run row, so there is no product_id to thread — the
    remover must be called with None (its rmtree path)."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    orphan = uuid.uuid4()
    (runs_root / str(orphan)).mkdir()
    old_ts = datetime.now(tz=UTC).timestamp() - 48 * 3600
    os.utime(runs_root / str(orphan), (old_ts, old_ts))

    seen: list[tuple] = []

    async def fake_remover(pid, rid):
        seen.append((pid, rid))

    async with sf() as s:
        await reap_terminal_run_workspaces(
            s, remover=fake_remover, runs_root=runs_root, orphan_grace_s=24 * 3600
        )
    assert seen == [(None, orphan)]


# ---------------------------------------------------------------------------
# reap_orphan_product_workspaces — bound var/products to live products
# ---------------------------------------------------------------------------


async def test_reap_orphan_product_workspaces(sf, tmp_path):
    """A product repo whose ProductRow is gone is dead weight forever — 18 such
    dirs (300MB, 90% of var/products) were found in production. Reap them past a
    grace window; keep live products and freshly-created dirs (the repo is
    provisioned right after the row commits, but a grace window costs nothing
    and removes the race entirely)."""
    products_root = tmp_path / "products"
    products_root.mkdir()

    live = uuid.uuid4()
    ws_id = uuid.uuid4()
    async with sf() as s:
        # PG enforces the products.workspace_id FK — seed the parent first.
        s.add(
            WorkspaceRow(
                id=ws_id,
                name="test-ws",
                safe_mode=False,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            ProductRow(
                id=live,
                workspace_id=ws_id,
                name="Live",
                slug="live",
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    old_orphan = uuid.uuid4()
    fresh_orphan = uuid.uuid4()
    for pid in (live, old_orphan, fresh_orphan):
        (products_root / str(pid)).mkdir()
    old_ts = datetime.now(tz=UTC).timestamp() - 48 * 3600
    os.utime(products_root / str(old_orphan), (old_ts, old_ts))

    removed: list[uuid.UUID] = []

    async def fake_remover(pid):
        removed.append(pid)

    async with sf() as s:
        reaped = await reap_orphan_product_workspaces(
            s, remover=fake_remover, products_root=products_root, grace_s=24 * 3600
        )

    assert reaped == [old_orphan]
    assert removed == [old_orphan]
    assert live not in reaped, "a live product's repo must never be reaped"
    assert fresh_orphan not in reaped, "a fresh dir may be mid-provision — keep"


async def test_reap_orphan_product_workspaces_no_root_is_noop(sf, tmp_path):
    async with sf() as s:
        assert (await reap_orphan_product_workspaces(s, products_root=tmp_path / "nope")) == []
