"""The awaiter stops waiting when its caller no longer wants the result (#1106).

Measured 2026-09-30: run ``58a7426d`` was cancelled at 10:41:01, and its executor task ran on to
10:52:22 and 6,403,534 prompt tokens. The backend was still parked in ``await_completion`` for
that task, polling the task row every two seconds — and nothing in that poll asked whether the
run it was working for still existed. The kill path to the worker was already built (the
timeout path uses it); the wait simply had no reason to take it before the deadline.

``abandon_if`` is that reason. The awaiter asks it on every poll tick; a ``True`` ends the wait
with :class:`TaskAbandoned` and closes the row, so it stops reading as a task in flight.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from .test_awaiter_redelivers_an_unclaimed_task import _engine_and_sessions, _seed, _SilentRedis

pytestmark = pytest.mark.asyncio


async def _status_of(sm: Any, task_id: uuid.UUID) -> tuple[str, str | None]:
    from backend.executors.db import ExecutorTaskRow

    async with sm() as session:
        row = await session.get(ExecutorTaskRow, task_id)
        assert row is not None
        return row.status, row.error_message


async def test_a_true_probe_ends_the_wait_and_closes_the_row() -> None:
    from backend.executors import dispatch

    engine, sm = await _engine_and_sessions()
    task_id, _ = await _seed(sm, claimed=True)
    asked: list[int] = []

    async def _cancelled() -> bool:
        asked.append(1)
        return len(asked) >= 2  # alive on the first tick, cancelled on the second

    started = asyncio.get_event_loop().time()
    try:
        async with sm() as session:
            with pytest.raises(dispatch.TaskAbandoned):
                await dispatch.await_completion(
                    _SilentRedis(),
                    session=session,
                    task_id=task_id,
                    timeout_s=60,
                    session_factory=sm,
                    abandon_if=_cancelled,
                )
        assert asyncio.get_event_loop().time() - started < 10, "must not wait out the deadline"
        assert len(asked) == 2
        status, error = await _status_of(sm, task_id)
        assert status == "failed"
        assert error is not None and "abandon" in error
    finally:
        await engine.dispose()


async def test_a_false_probe_leaves_the_wait_alone() -> None:
    """Control: an awaiter whose caller still wants the result times out as before."""
    from backend.executors import dispatch

    engine, sm = await _engine_and_sessions()
    task_id, _ = await _seed(sm, claimed=True)

    async def _alive() -> bool:
        return False

    try:
        async with sm() as session:
            with pytest.raises(dispatch.TaskTimeout):
                await dispatch.await_completion(
                    _SilentRedis(),
                    session=session,
                    task_id=task_id,
                    timeout_s=0.3,
                    session_factory=sm,
                    abandon_if=_alive,
                )
    finally:
        await engine.dispose()


async def test_a_probe_that_raises_does_not_kill_the_wait() -> None:
    """The probe is a DB read; a blip in it must not abandon a working session."""
    from backend.executors import dispatch

    engine, sm = await _engine_and_sessions()
    task_id, _ = await _seed(sm, claimed=True)

    async def _broken() -> bool:
        raise RuntimeError("db blip")

    try:
        async with sm() as session:
            with pytest.raises(dispatch.TaskTimeout):
                await dispatch.await_completion(
                    _SilentRedis(),
                    session=session,
                    task_id=task_id,
                    timeout_s=0.3,
                    session_factory=sm,
                    abandon_if=_broken,
                )
    finally:
        await engine.dispose()


async def test_a_result_that_is_already_in_wins_over_the_probe() -> None:
    """A task that finished is reported, even if its run was cancelled meanwhile —
    its tokens were spent and the row says so."""
    from backend.executors import dispatch

    engine, sm = await _engine_and_sessions()
    task_id, _ = await _seed(sm, claimed=True, status="done")

    async def _cancelled() -> bool:
        return True

    try:
        async with sm() as session:
            row = await dispatch.await_completion(
                _SilentRedis(),
                session=session,
                task_id=task_id,
                timeout_s=5,
                session_factory=sm,
                abandon_if=_cancelled,
            )
        assert row.status == "done"
    finally:
        await engine.dispose()
