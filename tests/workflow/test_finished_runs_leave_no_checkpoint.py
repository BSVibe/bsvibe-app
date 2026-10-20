"""#929 (B), stage 1 PR 5 — a finished run leaves no checkpoint behind off the machine.

``remove_run_worktree`` deletes a run's checkpoint on the machine that holds its tree. A run
that finishes elsewhere — or whose machine died after a checkpoint, then was cancelled — has no
tree anywhere to remove, so its ``runs/<rid>.bundle`` would sit in R2 forever. The store cannot
list, so the sweep is driven from the DB: a terminal run that still records a checkpoint has it
deleted, then the record is dropped. A run still in flight keeps its checkpoint; a failed delete
keeps the record, so the next sweep retries.

See docs/design/stateless-workers.md §2.5.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.workflow.application.run_checkpoint import CHECKPOINT_KEY
from backend.workflow.application.run_cleanup import reap_terminal_run_checkpoints
from backend.workflow.infrastructure.db import ExecutionBase, ExecutionRun, RunStatus

from .._support import db_engine


@pytest_asyncio.fixture
async def sf():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(sf_, status: RunStatus, payload: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID]:
    ws, rid = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with sf_() as s, workspace_session_scope(s, ws):
        s.add(
            ExecutionRun(
                id=rid,
                workspace_id=ws,
                status=status,
                payload=payload,
                created_at=now,
                updated_at=now,
            )
        )
        await s.commit()
    return ws, rid


async def _payload(sf_, ws: uuid.UUID, rid: uuid.UUID) -> dict[str, Any]:
    async with sf_() as s, workspace_session_scope(s, ws):
        run = await s.get(ExecutionRun, rid)
        assert run is not None
        return dict(run.payload)


_CP = {CHECKPOINT_KEY: {"sha": "abc", "machine": "box-a"}}


class _Deleter:
    def __init__(self, failing: set[uuid.UUID] | None = None) -> None:
        self.deleted: list[uuid.UUID] = []
        self.failing = failing or set()

    async def __call__(self, run_id: uuid.UUID) -> None:
        if run_id in self.failing:
            raise RuntimeError("R2 down")
        self.deleted.append(run_id)


async def test_every_finished_runs_checkpoint_is_deleted_and_forgotten(sf) -> None:
    runs = [
        await _seed(sf, status, {**_CP, "kind": "build"})
        for status in (RunStatus.SHIPPED, RunStatus.FAILED, RunStatus.CANCELLED)
    ]
    deleter = _Deleter()

    async with sf() as s:
        swept = await reap_terminal_run_checkpoints(s, deleter=deleter)
        await s.commit()

    assert sorted(swept) == sorted(r for _, r in runs) == sorted(deleter.deleted)
    for ws, rid in runs:
        assert await _payload(sf, ws, rid) == {"kind": "build"}, "only the record goes"


async def test_a_run_in_flight_keeps_its_checkpoint(sf) -> None:
    ws, rid = await _seed(sf, RunStatus.OPEN, dict(_CP))
    deleter = _Deleter()

    async with sf() as s:
        assert await reap_terminal_run_checkpoints(s, deleter=deleter) == []
        await s.commit()

    assert deleter.deleted == [] and CHECKPOINT_KEY in await _payload(sf, ws, rid)


async def test_a_finished_run_without_a_checkpoint_costs_no_delete(sf) -> None:
    await _seed(sf, RunStatus.SHIPPED, {"kind": "build"})
    deleter = _Deleter()

    async with sf() as s:
        assert await reap_terminal_run_checkpoints(s, deleter=deleter) == []

    assert deleter.deleted == []


async def test_a_failed_delete_is_retried_next_sweep(sf) -> None:
    ws_bad, bad = await _seed(sf, RunStatus.SHIPPED, dict(_CP))
    _, good = await _seed(sf, RunStatus.SHIPPED, dict(_CP))

    async with sf() as s:
        swept = await reap_terminal_run_checkpoints(s, deleter=_Deleter(failing={bad}))
        await s.commit()
    assert swept == [good], "one failure does not stop the sweep"
    assert CHECKPOINT_KEY in await _payload(sf, ws_bad, bad), "kept, so the next sweep retries"

    async with sf() as s:
        assert await reap_terminal_run_checkpoints(s, deleter=_Deleter()) == [bad]
        await s.commit()


async def test_the_worker_sweep_deletes_from_the_run_store(sf, monkeypatch) -> None:
    """The real worker sweep, with only the R2 call stubbed."""
    from backend.storage import product_workspace
    from backend.workflow.infrastructure.workers import agent_worker
    from backend.workflow.infrastructure.workers.agent_worker import AgentWorker

    deleted: list[uuid.UUID] = []

    async def _delete(run_id: uuid.UUID, *, store: Any = None) -> None:
        deleted.append(run_id)

    monkeypatch.setattr(product_workspace, "delete_run_checkpoint", _delete)
    monkeypatch.setattr(agent_worker, "_WORKSPACE_REAP_INTERVAL_S", 0.0)
    ws, rid = await _seed(sf, RunStatus.SHIPPED, dict(_CP))

    await AgentWorker(session_factory=sf)._reap_terminal_run_workspaces()

    assert deleted == [rid]
    assert CHECKPOINT_KEY not in await _payload(sf, ws, rid)
