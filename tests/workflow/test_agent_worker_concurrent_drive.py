"""AgentWorker drives the runs it claims CONCURRENTLY, bounded by ``agent_drive_concurrency``.

A real worker over a real database; only the drive itself (``_frame_and_drive_run``) is a
fake that waits on an event, so the tests can see whether two drives overlap in time.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.notifications.db  # noqa: F401 - register the table on the shared Base
from backend.data.rls import cross_tenant_session_read, workspace_session_scope
from backend.data.scoping import current_workspace_id
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_open_run(sf: Any, workspace_id: uuid.UUID | None = None) -> uuid.UUID:
    async with sf() as session:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id or uuid.uuid4(),
            product_id=None,
            request_id=None,
            status=RunStatus.OPEN,
            payload={},
        )
        async with workspace_session_scope(session, run.workspace_id):
            session.add(run)
            await session.flush()
        await session.commit()
        return run.id


async def _status(sf: Any, run_id: uuid.UUID) -> tuple[RunStatus, Any]:
    async with sf() as session, cross_tenant_session_read(session):
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        return run.status, run.claimed_at


def _worker(sf: Any, *, concurrency: int) -> Any:
    from backend.config import get_settings
    from backend.workflow.infrastructure.workers.agent_worker import (
        AgentWorker,
        AgentWorkerConfig,
    )

    settings = get_settings().model_copy(update={"agent_drive_concurrency": concurrency})
    worker = AgentWorker(
        session_factory=sf,
        config=AgentWorkerConfig(poll_interval_s=30.0),
        settings=settings,
    )
    worker._execution = object()  # type: ignore[assignment]

    async def _reap(*_a: Any, **_k: Any) -> int:
        return 0

    worker._reap_stale_claims = _reap  # type: ignore[method-assign]
    worker._reap_terminal_run_workspaces = _reap  # type: ignore[method-assign]
    return worker


async def _until(predicate: Any) -> None:
    async with asyncio.timeout(5):
        while not predicate():  # noqa: ASYNC110 - polling worker state, there is no event to await
            await asyncio.sleep(0.01)


async def test_two_open_runs_are_driven_at_the_same_time(sf) -> None:
    first, second = await _seed_open_run(sf), await _seed_open_run(sf)
    worker = _worker(sf, concurrency=2)
    release = asyncio.Event()
    started: list[uuid.UUID] = []
    finished: list[uuid.UUID] = []

    async def _drive(run_id: uuid.UUID, _execution: Any) -> None:
        started.append(run_id)
        await release.wait()
        finished.append(run_id)

    worker._frame_and_drive_run = _drive  # type: ignore[method-assign]

    driving = asyncio.create_task(worker.drive_once())
    await _until(lambda: len(started) == 2)
    assert finished == [], "the second drive started before the first one ended"
    assert {first, second} == set(started)

    release.set()
    assert await driving == 2


async def test_limit_one_drives_one_at_a_time(sf) -> None:
    await _seed_open_run(sf)
    await _seed_open_run(sf)
    worker = _worker(sf, concurrency=1)
    release = asyncio.Event()
    started: list[uuid.UUID] = []

    async def _drive(run_id: uuid.UUID, _execution: Any) -> None:
        started.append(run_id)
        await release.wait()

    worker._frame_and_drive_run = _drive  # type: ignore[method-assign]

    driving = asyncio.create_task(worker.drive_once())
    await _until(lambda: len(started) == 1)
    await asyncio.sleep(0.1)
    assert len(started) == 1, "a limit of 1 must not start a second drive"

    release.set()
    assert await driving == 1


async def test_runs_beyond_the_limit_are_not_claimed(sf) -> None:
    ids = [await _seed_open_run(sf) for _ in range(3)]
    worker = _worker(sf, concurrency=1)
    release = asyncio.Event()

    async def _drive(_run_id: uuid.UUID, _execution: Any) -> None:
        await release.wait()

    worker._frame_and_drive_run = _drive  # type: ignore[method-assign]

    tasks = await worker._start_drives()
    assert len(tasks) == 1
    # A full worker claims nothing more on the next tick.
    assert await worker._start_drives() == []

    statuses = [(await _status(sf, run_id))[0] for run_id in ids]
    assert statuses.count(RunStatus.RUNNING) == 1
    assert statuses.count(RunStatus.OPEN) == 2, "runs without a free slot stay OPEN"

    release.set()
    await asyncio.gather(*tasks)


async def test_tick_does_not_wait_for_drives(sf) -> None:
    run_id = await _seed_open_run(sf)
    worker = _worker(sf, concurrency=1)
    release = asyncio.Event()
    started = asyncio.Event()

    async def _drive(_run_id: uuid.UUID, _execution: Any) -> None:
        started.set()
        await release.wait()

    worker._frame_and_drive_run = _drive  # type: ignore[method-assign]

    async with asyncio.timeout(5):
        await worker._tick()  # returns while the drive is still running
        await started.wait()
    assert (await _status(sf, run_id))[0] is RunStatus.RUNNING

    release.set()
    await worker.stop()  # drains the drive in flight
    assert (await _status(sf, run_id))[1] is None, "a finished drive clears its claim"


async def test_a_failing_drive_does_not_stop_the_other(sf) -> None:
    bad, good = await _seed_open_run(sf), await _seed_open_run(sf)
    worker = _worker(sf, concurrency=2)
    gate = asyncio.Event()
    finished: list[uuid.UUID] = []

    async def _drive(run_id: uuid.UUID, _execution: Any) -> None:
        if run_id == bad:
            raise RuntimeError("turn timed out")
        await gate.wait()
        finished.append(run_id)

    worker._frame_and_drive_run = _drive  # type: ignore[method-assign]

    driving = asyncio.create_task(worker.drive_once())
    await _until(lambda: len(worker._drives) == 1)  # the failed one is already gone
    gate.set()
    assert await driving == 1

    assert finished == [good]
    bad_status, bad_claim = await _status(sf, bad)
    assert bad_claim is None, "the failed run's claim is released"
    assert bad_status is RunStatus.OPEN


async def test_concurrent_drives_are_scoped_to_their_own_workspace(sf) -> None:
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    run_a, run_b = await _seed_open_run(sf, ws_a), await _seed_open_run(sf, ws_b)
    worker = _worker(sf, concurrency=2)
    release = asyncio.Event()
    scope_seen: dict[uuid.UUID, list[uuid.UUID | None]] = {run_a: [], run_b: []}

    async def _drive(run_id: uuid.UUID, _execution: Any) -> None:
        scope_seen[run_id].append(current_workspace_id.get())
        await release.wait()  # the other drive runs meanwhile
        scope_seen[run_id].append(current_workspace_id.get())
        async with worker._session_factory() as session:
            run = await session.get(ExecutionRun, run_id)  # auto-filtered by workspace
            assert run is not None
            other = run_b if run_id == run_a else run_a
            assert await session.get(ExecutionRun, other) is None

    worker._frame_and_drive_run = _drive  # type: ignore[method-assign]

    driving = asyncio.create_task(worker.drive_once())
    await _until(lambda: all(len(v) == 1 for v in scope_seen.values()))
    release.set()
    assert await driving == 2

    assert scope_seen[run_a] == [ws_a, ws_a]
    assert scope_seen[run_b] == [ws_b, ws_b]
    assert current_workspace_id.get() is None
