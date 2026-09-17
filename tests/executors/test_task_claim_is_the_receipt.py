"""A dispatched task is only *received* once a worker claims it (#965).

Prod symptom: runs sat in ``dispatched`` until their awaiter's timeout, and
nobody could say whether the worker had the task and was slow, or had never
got it at all. The two are not distinguishable today because **nothing the
worker does is recorded between the XADD and the result**.

The delivery hop is lossier than the module's docstring claims. ``POST
/api/v1/workers/poll`` hands ``consume_once`` a handler that only appends to a
list — it *cannot* raise — so the XACK completes **before the HTTP response is
built**. A lost response, a proxy timeout, or a worker that dies on receipt
means the entry is acked and gone: there is no pending entry left to redeliver,
and the poll never passes ``min_idle_ms``, so the worker's stream is never
XAUTOCLAIMed either. That is the "delivery gap" (U1).

``claimed_at`` closes it by making receipt a **recorded, atomic** fact rather
than an inference from elapsed time. The claim is a conditional UPDATE, so:

* a task claimed once cannot be claimed again — the second claim matches 0 rows
  and the worker declines to run it. Duplicate protection comes from the
  ``WHERE``, not from a timer, so a turn that legitimately runs for an hour is
  never at risk of a duplicate (which is exactly what a lease-and-heartbeat
  design cannot promise);
* an *unclaimed* dispatched task is positively known never to have been picked
  up, which is what makes redelivery safe to attempt at all.

The claim inherits ``record_result``'s H1 binding: a worker may only claim a
task dispatched **to it**. Without that, any live worker token claims another
tenant's task and starves the run.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


async def _session_factory() -> Any:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.data import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, sm


async def _dispatched_task(session: Any, *, worker_id: uuid.UUID) -> Any:
    from backend.executors.db import ExecutorTaskRow

    task = ExecutorTaskRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        executor_type="claude_code",
        prompt="p",
        system="",
        workspace_dir="/srv/run",
        status="dispatched",
        worker_id=worker_id,
    )
    session.add(task)
    await session.flush()
    return task


async def test_first_claim_wins_and_stamps_the_receipt() -> None:
    from backend.executors import dispatch

    engine, sm = await _session_factory()
    try:
        worker_id = uuid.uuid4()
        async with sm() as session:
            task = await _dispatched_task(session, worker_id=worker_id)
            assert task.claimed_at is None, "a freshly dispatched task is unclaimed"

            claimed = await dispatch.claim_task(session, task_id=task.id, worker_id=worker_id)
            assert claimed is True, "the first claim must win"
            await session.commit()

        async with sm() as session:
            from backend.executors.db import ExecutorTaskRow

            row = await session.get(ExecutorTaskRow, task.id)
            assert row is not None
            assert row.claimed_at is not None, (
                "receipt must be RECORDED — an unstamped row is indistinguishable "
                "from one that was never delivered"
            )
    finally:
        await engine.dispose()


async def test_second_claim_of_the_same_task_is_refused() -> None:
    """This is the whole duplicate-protection argument — and it is structural.

    Not a time window: a claimed row simply no longer matches the ``WHERE``.
    """
    from backend.executors import dispatch

    engine, sm = await _session_factory()
    try:
        worker_id = uuid.uuid4()
        async with sm() as session:
            task = await _dispatched_task(session, worker_id=worker_id)
            first = await dispatch.claim_task(session, task_id=task.id, worker_id=worker_id)
            second = await dispatch.claim_task(session, task_id=task.id, worker_id=worker_id)
            assert first is True
            assert second is False, (
                "a redelivered copy of an already-running task must be declined; "
                "the worker does NOT dedupe execute messages on its own"
            )
    finally:
        await engine.dispose()


async def test_a_foreign_worker_cannot_claim(caplog: Any) -> None:
    """H1 binding — mirrors ``record_result``'s worker check.

    Without it a live token from any workspace claims a task it was never
    dispatched, and the real worker's claim then fails: a cross-tenant
    run-completion DoS with no result ever recorded.
    """
    from backend.executors import dispatch

    engine, sm = await _session_factory()
    try:
        owner = uuid.uuid4()
        intruder = uuid.uuid4()
        async with sm() as session:
            task = await _dispatched_task(session, worker_id=owner)
            stolen = await dispatch.claim_task(session, task_id=task.id, worker_id=intruder)
            assert stolen is False, "a task may only be claimed by the worker it went to"

            # and the rightful owner is still able to claim afterwards
            assert await dispatch.claim_task(session, task_id=task.id, worker_id=owner) is True
    finally:
        await engine.dispose()


async def test_a_terminal_task_cannot_be_claimed() -> None:
    """``status='dispatched'`` is part of the WHERE, as it is in ``record_result``.

    A late redelivery of a task that already reported must not be started.
    """
    from backend.executors import dispatch
    from backend.executors.db import ExecutorTaskRow

    engine, sm = await _session_factory()
    try:
        worker_id = uuid.uuid4()
        async with sm() as session:
            task = await _dispatched_task(session, worker_id=worker_id)
            task.status = "done"
            await session.flush()
            assert (
                await dispatch.claim_task(session, task_id=task.id, worker_id=worker_id)
            ) is False
            await session.commit()

        async with sm() as session:
            row = await session.get(ExecutorTaskRow, task.id)
            assert row is not None
            assert row.claimed_at is None
    finally:
        await engine.dispose()


async def test_claiming_an_unknown_task_is_refused_not_raised() -> None:
    """Same shape as ``record_result``'s unknown-task path: a quiet ``False``.

    A prober must not be able to tell "not yours" from "unknown".
    """
    from backend.executors import dispatch

    engine, sm = await _session_factory()
    try:
        async with sm() as session:
            assert (
                await dispatch.claim_task(session, task_id=uuid.uuid4(), worker_id=uuid.uuid4())
            ) is False
    finally:
        await engine.dispose()
