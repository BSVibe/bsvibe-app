"""#1110 — every run status change goes through ONE function.

#1102 made ``AgentRunner.transition`` a compare-and-set, but six other places
wrote ``run.status`` directly with their own history row: local auto-ship,
delivery auto-resolve (→ shipped), cancel, reopen, Safe-Mode-deny reopen and the
drive-failure escalation. None of them got the cancel guard or the CAS — a cancel
committed by another session could still be overwritten through any of them.

``move_run_status`` is now the one place a run's status changes: a conditional
``UPDATE … WHERE status = :from``, the history row only when it lands, and the
held object mirrored without a dirty write. The guard at the bottom pins that no
one writes ``run.status`` anywhere else.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.workflow.application.run_status import move_run_status
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunHistory, RunStatus

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(sf_, workspace_id: uuid.UUID, status: RunStatus) -> uuid.UUID:
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=status,
            payload={},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.commit()
        return run.id


async def _history(sf_, workspace_id: uuid.UUID, run_id: uuid.UUID) -> list[RunStatus]:
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        rows = await s.execute(
            select(ExecutionRunHistory.to_status).where(ExecutionRunHistory.run_id == run_id)
        )
        return list(rows.scalars())


async def test_a_move_lands_writes_history_and_mirrors_the_held_object(sf) -> None:
    workspace_id = uuid.uuid4()
    run_id = await _seed(sf, workspace_id, RunStatus.REVIEW_READY)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        held = await s.get(ExecutionRun, run_id)
        assert held is not None
        moved = await move_run_status(s, held, RunStatus.SHIPPED, reason="auto-shipped")
        await s.commit()
        assert moved is True
        assert held.status is RunStatus.SHIPPED

    assert await _history(sf, workspace_id, run_id) == [RunStatus.SHIPPED]


async def test_a_move_over_a_status_changed_elsewhere_is_refused(sf) -> None:
    workspace_id = uuid.uuid4()
    run_id = await _seed(sf, workspace_id, RunStatus.REVIEW_READY)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        held = await s.get(ExecutionRun, run_id)  # HELD — the identity map is weak
        assert held is not None
        await s.commit()

        async with sf() as other, workspace_session_scope(other, workspace_id):
            elsewhere = await other.get(ExecutionRun, run_id)
            assert elsewhere is not None
            assert await move_run_status(other, elsewhere, RunStatus.CANCELLED, reason="founder")
            await other.commit()

        moved = await move_run_status(s, held, RunStatus.SHIPPED, reason="auto-shipped")
        await s.commit()
        assert moved is False
        assert held.status is RunStatus.CANCELLED  # the caller sees the truth

    assert await _history(sf, workspace_id, run_id) == [RunStatus.CANCELLED]


async def test_moving_to_the_current_status_is_a_no_op(sf) -> None:
    workspace_id = uuid.uuid4()
    run_id = await _seed(sf, workspace_id, RunStatus.OPEN)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        held = await s.get(ExecutionRun, run_id)
        assert held is not None
        assert await move_run_status(s, held, RunStatus.OPEN, reason="x") is False
        await s.commit()

    assert await _history(sf, workspace_id, run_id) == []


# ── The guard: no run status is written anywhere else ─────────────────────────

_BACKEND = Path(__file__).resolve().parents[2] / "backend"
_HOME = _BACKEND / "workflow" / "application" / "run_status.py"
#: Bulk claims whose rows are locked by ``SELECT … FOR UPDATE SKIP LOCKED`` in the
#: same transaction — already a compare-and-set by construction. Listed by
#: (file, the status they set), so a NEW bulk write is still caught.
_LOCKED_BULK_CLAIMS = {
    ("workflow/infrastructure/workers/agent_worker.py", "OPEN"),  # stale-claim reaper
    ("workflow/infrastructure/workers/agent_worker.py", "RUNNING"),  # drive claim
}


def _violations() -> list[str]:
    found: list[str] = []
    for path in sorted(_BACKEND.rglob("*.py")):
        if path == _HOME:
            continue
        rel = path.relative_to(_BACKEND).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # ``run.status = …`` / ``<anything>.status = RunStatus.X``
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if not (isinstance(target, ast.Attribute) and target.attr == "status"):
                        continue
                    on_run = isinstance(target.value, ast.Name) and target.value.id == "run"
                    sets_run_status = (
                        isinstance(node.value, ast.Attribute)
                        and isinstance(node.value.value, ast.Name)
                        and node.value.value.id == "RunStatus"
                    )
                    if on_run or sets_run_status:
                        found.append(f"{rel}:{node.lineno} assigns .status")
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else None
                # A history row that records a MOVE (a creation has from_status=None).
                if name == "ExecutionRunHistory":
                    from_kw = next((k for k in node.keywords if k.arg == "from_status"), None)
                    creation = (
                        from_kw is not None
                        and isinstance(from_kw.value, ast.Constant)
                        and from_kw.value.value is None
                    )
                    if not creation:
                        found.append(f"{rel}:{node.lineno} writes a move history row")
                # ``.values(status=RunStatus.X)`` on a bulk UPDATE
                if isinstance(node.func, ast.Attribute) and node.func.attr == "values":
                    for kw in node.keywords:
                        if (
                            kw.arg == "status"
                            and isinstance(kw.value, ast.Attribute)
                            and isinstance(kw.value.value, ast.Name)
                            and kw.value.value.id == "RunStatus"
                            and (rel, kw.value.attr) not in _LOCKED_BULK_CLAIMS
                        ):
                            found.append(f"{rel}:{node.lineno} bulk-writes status")
    return found


def test_no_run_status_is_written_outside_the_one_function() -> None:
    assert _violations() == []


def test_the_guard_can_see_a_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control — the scan must be able to go red, or it measures nothing."""
    fake = tmp_path / "backend"
    (fake / "workflow" / "application").mkdir(parents=True)
    (fake / "x.py").write_text("def f(run):\n    run.status = RunStatus.SHIPPED\n")
    monkeypatch.setattr(__import__(__name__, fromlist=["_BACKEND"]), "_BACKEND", fake)
    assert _violations() == ["x.py:2 assigns .status"]
