"""A worker restart gives its drives back instead of stranding them for the lease.

Prod 2026-11-05 (#1322, run 04a2a7eb): the run was claimed at 08:10:51; the deploy of #1321
restarted the worker at 08:11. ``stop()`` waited for the drive, the container was killed before
the drive's cancel path could release the claim, and the run sat RUNNING with a claim nobody
held — for the stale-claim lease, 2 × ``executor_task_timeout_s`` = two hours.

Two layers: ``stop()`` cancels the drives in flight (their cancel path gives the claim back),
and a starting worker takes back the claims a previous process left on ITS machine.

A real worker over a real database; only the drive itself is a fake.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.notifications.db  # noqa: F401 - register the table on the shared Base
from backend.data.rls import cross_tenant_session_read, workspace_session_scope
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_run(
    sf: Any,
    *,
    status: RunStatus = RunStatus.OPEN,
    claimed_by: uuid.UUID | None = None,
    home_machine: str | None = None,
    pending_decision: bool = False,
) -> uuid.UUID:
    claimed = status is RunStatus.RUNNING and claimed_by is not None
    async with sf() as session:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            product_id=None,
            request_id=None,
            status=status,
            payload={},
            claimed_at=datetime.now(UTC) if claimed else None,
            claimed_by=claimed_by if claimed else None,
            home_machine=home_machine if claimed else None,
        )
        async with workspace_session_scope(session, run.workspace_id):
            session.add(run)
            await session.flush()
            if pending_decision:
                session.add(
                    Decision(
                        id=uuid.uuid4(),
                        run_id=run.id,
                        workspace_id=run.workspace_id,
                        decision="ask_user_question",
                        status=DecisionStatus.PENDING,
                        payload={},
                    )
                )
        await session.commit()
        return run.id


async def _state(sf: Any, run_id: uuid.UUID) -> tuple[RunStatus, Any, Any]:
    async with sf() as session, cross_tenant_session_read(session):
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        return run.status, run.claimed_at, run.claimed_by


def _worker(sf: Any, *, machine_id: str = "") -> Any:
    from backend.config import get_settings
    from backend.workflow.infrastructure.workers.agent_worker import (
        AgentWorker,
        AgentWorkerConfig,
    )

    settings = get_settings().model_copy(
        update={"agent_drive_concurrency": 2, "machine_id": machine_id}
    )
    worker = AgentWorker(
        session_factory=sf,
        config=AgentWorkerConfig(poll_interval_s=30.0),
        settings=settings,
    )
    worker._execution = object()  # type: ignore[assignment]

    async def _none(*_a: Any, **_k: Any) -> int:
        return 0

    worker._reap_terminal_run_workspaces = _none  # type: ignore[method-assign]

    async def _no_checkpoint(*_a: Any, **_k: Any) -> None:
        return None

    worker._checkpoint_before_release = _no_checkpoint  # type: ignore[method-assign]
    return worker


async def _until(predicate: Any) -> None:
    async with asyncio.timeout(5):
        while not predicate():  # noqa: ASYNC110 - polling worker state, there is no event to await
            await asyncio.sleep(0.01)


async def test_stop_gives_a_drive_in_flight_back_without_waiting_for_it(sf) -> None:
    run_id = await _seed_run(sf)
    worker = _worker(sf)
    started = asyncio.Event()

    async def _drive(_run_id: uuid.UUID, _execution: Any) -> None:
        started.set()
        await asyncio.Event().wait()  # a long executor turn: never ends on its own

    worker._frame_and_drive_run = _drive  # type: ignore[method-assign]
    await worker._start_drives()
    await asyncio.wait_for(started.wait(), 5)
    assert (await _state(sf, run_id))[0] is RunStatus.RUNNING

    async with asyncio.timeout(5):  # a deploy's grace period, not the turn's hour
        await worker.stop()

    status, claimed_at, claimed_by = await _state(sf, run_id)
    assert status is RunStatus.OPEN, "the run goes back to the queue for the next worker"
    assert claimed_at is None and claimed_by is None
    assert not worker._drives


async def test_stop_right_after_a_claim_still_gives_the_run_back(sf) -> None:
    """A drive cancelled before it ever ran has no cancel path of its own."""
    run_id = await _seed_run(sf)
    worker = _worker(sf)

    async def _drive(_run_id: uuid.UUID, _execution: Any) -> None:
        raise AssertionError("never reached: stopped before the drive began")

    worker._frame_and_drive_run = _drive  # type: ignore[method-assign]
    tasks = await worker._start_drives()
    assert len(tasks) == 1
    await worker.stop()  # no await in between: the drive task has not started

    status, claimed_at, _ = await _state(sf, run_id)
    assert status is RunStatus.OPEN
    assert claimed_at is None


async def test_a_starting_worker_takes_back_claims_a_previous_process_left(sf) -> None:
    gone = uuid.uuid4()  # the killed process's worker id
    orphan = await _seed_run(sf, status=RunStatus.RUNNING, claimed_by=gone)
    worker = _worker(sf)
    driven: list[uuid.UUID] = []

    async def _drive(run_id: uuid.UUID, _execution: Any) -> None:
        driven.append(run_id)

    worker._frame_and_drive_run = _drive  # type: ignore[method-assign]
    tasks = await worker._start_drives()
    await asyncio.gather(*tasks)

    assert driven == [orphan], "the orphaned run is driven at once, not after the lease"


async def test_startup_leaves_other_machines_paused_runs_and_its_own_claims(sf) -> None:
    other_box = await _seed_run(
        sf, status=RunStatus.RUNNING, claimed_by=uuid.uuid4(), home_machine="box-b"
    )
    paused = await _seed_run(sf, status=RunStatus.RUNNING, pending_decision=True)
    gone_here = await _seed_run(
        sf, status=RunStatus.RUNNING, claimed_by=uuid.uuid4(), home_machine="box-a"
    )
    worker = _worker(sf, machine_id="box-a")

    assert await worker._release_orphaned_claims() == 1

    assert (await _state(sf, gone_here))[0] is RunStatus.OPEN
    assert (await _state(sf, other_box))[0] is RunStatus.RUNNING, "another box's live drive"
    assert (await _state(sf, paused))[0] is RunStatus.RUNNING, "paused on a founder decision"

    # Its own claims are never orphans.
    mine = await _seed_run(
        sf, status=RunStatus.RUNNING, claimed_by=worker._worker_id, home_machine="box-a"
    )
    assert await worker._release_orphaned_claims() == 0
    assert (await _state(sf, mine))[0] is RunStatus.RUNNING
