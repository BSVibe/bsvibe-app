"""#967 — a timeout says WHICH tenant scope it read under, and what an unscoped read sees.

Measured (#967, 2026-09-15): with the workspace contextvar deliberately leaked to another
workspace, ``test_client_worker_manager`` failed 11 of 19 with the same
``exec timed out … last status 'dispatched'`` a starved runner produces. Two different
failures — "the result never arrived" and "the row is hidden from this scope" — printed one
message, so each red cost an investigation of both.

Now the message also carries:

* ``scope=<workspace or none>`` — the tenant the awaiter's reads were filtered to;
* ``unscoped <status>`` — the row as a read with no tenant filter sees it.

A scoped "not visible" next to an unscoped status is a scope problem, on one line. A
matching pair is the old "never reported" shape.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import backend.executors.db  # noqa: F401
from backend.data.scoping import workspace_scope
from backend.executors import dispatch

from .._support import memory_session

pytestmark = pytest.mark.asyncio


async def _redis() -> Any:
    import fakeredis.aioredis as fakeredis_aio

    return fakeredis_aio.FakeRedis(decode_responses=True)


async def test_an_unscoped_timeout_says_so() -> None:
    redis = await _redis()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=uuid.uuid4(), executor_type="claude_code", prompt="p"
        )
        await s.commit()
        with pytest.raises(dispatch.TaskTimeout) as exc:
            await dispatch.await_completion(redis, session=s, task_id=task.id, timeout_s=0.3)

    assert exc.value.scope is None
    assert exc.value.unscoped_status == task.status
    assert "scope=none" in str(exc.value)
    assert f"unscoped {task.status!r}" in str(exc.value)
    await redis.aclose()


async def test_a_row_hidden_by_the_scope_reads_as_a_scope_problem() -> None:
    """The leak shape: the awaiter is filtered to ANOTHER workspace. Its own reads see
    nothing; an unscoped read sees the row — and the message says both."""
    redis = await _redis()
    other = uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=uuid.uuid4(), executor_type="claude_code", prompt="p"
        )
        await s.commit()
        # The identity map would answer ``get`` without a query — and without the filter.
        s.expunge_all()
        with workspace_scope(other), pytest.raises(dispatch.TaskTimeout) as exc:
            await dispatch.await_completion(redis, session=s, task_id=task.id, timeout_s=0.3)

    assert exc.value.last_status is None, "the scoped reads must not have seen it"
    assert exc.value.scope == other
    assert exc.value.unscoped_status == task.status
    assert f"scope={other}" in str(exc.value)
    await redis.aclose()


async def test_a_row_that_does_not_exist_is_not_visible_either_way() -> None:
    """Control — nothing to find: both reads say so, and nothing invents a status."""
    redis = await _redis()
    async with memory_session() as s:
        with pytest.raises(dispatch.TaskTimeout) as exc:
            await dispatch.await_completion(redis, session=s, task_id=uuid.uuid4(), timeout_s=0.3)

    assert exc.value.last_status is None
    assert exc.value.unscoped_status is None
    assert "unscoped not visible" in str(exc.value)
    await redis.aclose()


async def test_the_scope_is_put_back_after_the_unscoped_read() -> None:
    """The diagnosis must not leak a scope change of its own — that is the very bug it reports."""
    from backend.data.scoping import current_workspace_id

    redis = await _redis()
    mine = uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=mine, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        with workspace_scope(mine):
            with pytest.raises(dispatch.TaskTimeout):
                await dispatch.await_completion(redis, session=s, task_id=task.id, timeout_s=0.3)
            assert current_workspace_id.get() == mine
    await redis.aclose()
