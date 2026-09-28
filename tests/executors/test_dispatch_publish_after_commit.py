"""A task is published to the worker's stream only once its row is committed.

CI 2026-09-28 (run 36402552114, #950): the fake worker read the stream 5 ms
after ``executor_task_dispatched`` and reported — but ``record_result`` found no
row (``task is None`` → ``return None``, unlogged), because ``dispatch_task``
XADDed BEFORE its caller committed. The task stayed ``dispatched`` and the
awaiter timed out 90 s later. A real worker that beats the commit loses the task
the same way; both callers (the executor adapter and the sandbox exec) had that
order.

These use a FILE-backed SQLite with a connection per session, so "another
session can see it" means committed — a shared in-memory connection would see
the uncommitted row and prove nothing.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

import backend.executors.db  # noqa: F401
from backend.executors import dispatch
from backend.executors.db import ExecutorTaskRow

from .._support import shared_file_sessionmaker
from .test_dispatch import _make_redis, _seed_worker

pytestmark = pytest.mark.asyncio


class _WitnessRedis:
    """Delegates to fakeredis; at XADD time, looks at the row from ANOTHER session."""

    def __init__(self, inner: Any, factory: async_sessionmaker[AsyncSession]) -> None:
        self._inner = inner
        self._factory = factory
        self.seen_at_publish: tuple[str, uuid.UUID | None] | None = None
        self.fail: bool = False

    async def xadd(self, stream: str, fields: dict[str, str], **kw: Any) -> str:
        async with self._factory() as other:
            row = await other.get(ExecutorTaskRow, uuid.UUID(fields["task_id"]))
            self.seen_at_publish = None if row is None else (row.status, row.worker_id)
        if self.fail:
            raise ConnectionError("redis down")
        return await self._inner.xadd(stream, fields, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _task_and_worker(
    factory: async_sessionmaker[AsyncSession], s: AsyncSession
) -> tuple[ExecutorTaskRow, uuid.UUID]:
    workspace_id = uuid.uuid4()
    worker = await _seed_worker(s, workspace_id=workspace_id, capabilities=["claude_code"])
    worker_id = worker.id
    await s.commit()
    # The real callers' shape: create (flush only) and dispatch in ONE transaction.
    task = await dispatch.create_task(
        s, workspace_id=workspace_id, executor_type="claude_code", prompt="ls"
    )
    return task, worker_id


async def test_the_row_is_committed_as_dispatched_before_the_stream_sees_it() -> None:
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        witness = _WitnessRedis(redis, factory)
        async with factory() as s:
            task, worker_id = await _task_and_worker(factory, s)
            await dispatch.dispatch_task(witness, session=s, task=task, worker_id=worker_id)

        assert witness.seen_at_publish == ("dispatched", worker_id)
    await redis.aclose()


async def test_a_failed_publish_does_not_leave_a_task_that_looks_in_flight() -> None:
    """Committing first means a publish failure can no longer roll the row away.
    It must not stay ``dispatched`` — that reads as work a worker holds, and the
    awaiter would sit out its whole timeout for a task nobody ever received."""
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        witness = _WitnessRedis(redis, factory)
        witness.fail = True
        async with factory() as s:
            task, worker_id = await _task_and_worker(factory, s)
            task_id = task.id
            with pytest.raises(ConnectionError):
                await dispatch.dispatch_task(witness, session=s, task=task, worker_id=worker_id)

        async with factory() as other:
            row = await other.get(ExecutorTaskRow, task_id)
            assert row is not None
            assert row.status == "failed"
            assert row.error_message
    await redis.aclose()


async def test_a_result_for_a_task_that_is_not_there_says_so() -> None:
    """The flake's last cell was hidden by an unlogged ``return None``."""
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        async with factory() as s:
            with capture_logs() as logs:
                out = await dispatch.record_result(
                    s,
                    redis,
                    task_id=uuid.uuid4(),
                    worker_id=uuid.uuid4(),
                    success=True,
                    output="",
                    error_message=None,
                )
        assert out is None
        assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
            "executor_task_result_for_unknown_task"
        ]
    await redis.aclose()
