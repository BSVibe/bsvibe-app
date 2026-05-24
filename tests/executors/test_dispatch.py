"""Service-layer tests for the executor dispatch substrate (Lift 2).

Lift 2 of the executor-pool epic — the dispatch / poll / result substrate,
ported from BSGateway (``executor/dispatcher.py`` + ``chat/service.py``) and
adapted to async SQLAlchemy + ``workspace_id``.

These exercise :mod:`backend.executors.dispatch` directly against an in-memory
SQLite session (the unit tier) plus a ``fakeredis`` double for the Redis Streams
+ pub/sub surfaces — NO HTTP layer, NO real worker process. The "worker" is
simulated by calling :func:`record_result` + publishing the done channel.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

# Importing the module db registers the tables on the shared Base.metadata so
# ``memory_session``'s create_all materialises them.
import backend.executors.db  # noqa: F401
from backend.executors import dispatch, service
from backend.executors.db import ExecutorTaskRow, WorkerRow

from .._support import memory_session

pytestmark = pytest.mark.asyncio


async def _make_redis() -> Any:
    """Return a connected fakeredis client, or skip when unavailable."""
    try:
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:  # pragma: no cover - fakeredis is a declared dep
        pytest.skip("fakeredis not installed")
    client = fakeredis_aio.FakeRedis(decode_responses=True)
    await client.flushdb()
    return client


async def _seed_worker(
    s: Any,
    *,
    workspace_id: uuid.UUID,
    capabilities: list[str],
    status: str = "online",
    heartbeat_age_s: float | None = 0.0,
) -> WorkerRow:
    """Insert a worker row directly (bypassing the install-token flow)."""
    last_heartbeat = (
        None if heartbeat_age_s is None else datetime.now(UTC) - timedelta(seconds=heartbeat_age_s)
    )
    worker = WorkerRow(
        workspace_id=workspace_id,
        name="w",
        labels=[],
        capabilities=list(capabilities),
        status=status,
        last_heartbeat=last_heartbeat,
        token_hash=service._hash_token(uuid.uuid4().hex),
        is_active=True,
    )
    s.add(worker)
    await s.flush()
    return worker


# ── channel naming ────────────────────────────────────────────────────────────


async def test_channel_helpers() -> None:
    task_id = uuid.uuid4()
    assert dispatch.stream_channel(task_id) == f"task:{task_id}:stream"
    assert dispatch.done_channel(task_id) == f"task:{task_id}:done"


async def test_worker_stream_name() -> None:
    worker_id = uuid.uuid4()
    assert dispatch.worker_stream(worker_id) == f"tasks:worker:{worker_id}"


# ── create_task ────────────────────────────────────────────────────────────────


async def test_create_task_starts_pending() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s,
            workspace_id=workspace_id,
            executor_type="claude_code",
            prompt="do the thing",
            system="be terse",
            workspace_dir="/srv/work",
        )
        await s.commit()
        assert task.workspace_id == workspace_id
        assert task.executor_type == "claude_code"
        assert task.prompt == "do the thing"
        assert task.system == "be terse"
        assert task.workspace_dir == "/srv/work"
        assert task.status == "pending"
        assert task.worker_id is None
        assert task.output == ""
        assert task.error_message is None


async def test_create_task_defaults() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s,
            workspace_id=workspace_id,
            executor_type="codex",
            prompt="p",
        )
        await s.commit()
        assert task.system == ""
        assert task.workspace_dir == "."
        assert task.status == "pending"


# ── find_available_worker ──────────────────────────────────────────────────────


async def test_find_available_worker_matches_capability_and_fresh_heartbeat() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        worker = await _seed_worker(
            s, workspace_id=workspace_id, capabilities=["claude_code"], heartbeat_age_s=5
        )
        await s.commit()
        found = await dispatch.find_available_worker(
            s, workspace_id=workspace_id, executor_type="claude_code"
        )
        assert found is not None
        assert found.id == worker.id


async def test_find_available_worker_excludes_missing_capability() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        await _seed_worker(s, workspace_id=workspace_id, capabilities=["codex"], heartbeat_age_s=5)
        await s.commit()
        found = await dispatch.find_available_worker(
            s, workspace_id=workspace_id, executor_type="claude_code"
        )
        assert found is None


async def test_find_available_worker_excludes_stale_heartbeat() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        await _seed_worker(
            s, workspace_id=workspace_id, capabilities=["claude_code"], heartbeat_age_s=300
        )
        await s.commit()
        found = await dispatch.find_available_worker(
            s, workspace_id=workspace_id, executor_type="claude_code"
        )
        assert found is None


async def test_find_available_worker_excludes_offline() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        await _seed_worker(
            s,
            workspace_id=workspace_id,
            capabilities=["claude_code"],
            status="offline",
            heartbeat_age_s=5,
        )
        await s.commit()
        found = await dispatch.find_available_worker(
            s, workspace_id=workspace_id, executor_type="claude_code"
        )
        assert found is None


async def test_find_available_worker_excludes_never_heartbeated() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        await _seed_worker(
            s,
            workspace_id=workspace_id,
            capabilities=["claude_code"],
            heartbeat_age_s=None,
        )
        await s.commit()
        found = await dispatch.find_available_worker(
            s, workspace_id=workspace_id, executor_type="claude_code"
        )
        assert found is None


async def test_find_available_worker_is_workspace_scoped() -> None:
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    async with memory_session() as s:
        await _seed_worker(s, workspace_id=ws_a, capabilities=["claude_code"], heartbeat_age_s=5)
        await s.commit()
        found = await dispatch.find_available_worker(
            s, workspace_id=ws_b, executor_type="claude_code"
        )
        assert found is None


async def test_find_available_worker_pinned_accepts_stale_heartbeat() -> None:
    """A pinned worker is accepted even with a stale heartbeat (caller bound it)."""
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        worker = await _seed_worker(
            s,
            workspace_id=workspace_id,
            capabilities=["claude_code"],
            status="offline",
            heartbeat_age_s=9999,
        )
        await s.commit()
        found = await dispatch.find_available_worker(
            s,
            workspace_id=workspace_id,
            executor_type="claude_code",
            pinned_worker_id=worker.id,
        )
        assert found is not None
        assert found.id == worker.id


async def test_find_available_worker_pinned_missing_capability_falls_through() -> None:
    """A pinned worker lacking the capability falls through to the normal scan."""
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        pinned = await _seed_worker(
            s, workspace_id=workspace_id, capabilities=["codex"], heartbeat_age_s=5
        )
        fresh = await _seed_worker(
            s, workspace_id=workspace_id, capabilities=["claude_code"], heartbeat_age_s=5
        )
        await s.commit()
        found = await dispatch.find_available_worker(
            s,
            workspace_id=workspace_id,
            executor_type="claude_code",
            pinned_worker_id=pinned.id,
        )
        assert found is not None
        assert found.id == fresh.id


async def test_mark_pending_resets_task() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        worker = await _seed_worker(
            s, workspace_id=workspace_id, capabilities=["claude_code"], heartbeat_age_s=5
        )
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        # Pretend it was dispatched, then reset.
        task.status = "dispatched"
        task.worker_id = worker.id
        await s.flush()
        await dispatch.mark_pending(s, task_id=task.id)
        await s.commit()
        refreshed = await s.get(ExecutorTaskRow, task.id)
        assert refreshed is not None
        assert refreshed.status == "pending"
        assert refreshed.worker_id is None


# ── dispatch_task ────────────────────────────────────────────────────────────


async def test_dispatch_task_xadds_and_marks_dispatched() -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        worker = await _seed_worker(
            s, workspace_id=workspace_id, capabilities=["claude_code"], heartbeat_age_s=5
        )
        task = await dispatch.create_task(
            s,
            workspace_id=workspace_id,
            executor_type="claude_code",
            prompt="hello",
            system="sys",
            workspace_dir="/work",
        )
        await s.commit()

        msg_id = await dispatch.dispatch_task(redis, session=s, task=task, worker_id=worker.id)
        await s.commit()
        assert msg_id

        # The task row is now dispatched + bound to the worker.
        refreshed = await s.get(ExecutorTaskRow, task.id)
        assert refreshed is not None
        assert refreshed.status == "dispatched"
        assert refreshed.worker_id == worker.id

        # The message landed on the worker's dedicated stream with the expected
        # flat-string payload.
        entries = await redis.xrange(dispatch.worker_stream(worker.id))
        assert len(entries) == 1
        _entry_id, fields = entries[0]
        assert fields["task_id"] == str(task.id)
        assert fields["executor_type"] == "claude_code"
        assert fields["prompt"] == "hello"
        assert fields["system"] == "sys"
        assert fields["workspace_dir"] == "/work"
        assert fields["action"] == "execute"
        assert fields["stream_channel"] == dispatch.stream_channel(task.id)
        assert fields["done_channel"] == dispatch.done_channel(task.id)
        assert fields["dispatched_at"]
        # Every stream field must be a flat string (Redis Streams constraint).
        assert all(isinstance(v, str) for v in fields.values())
    await redis.aclose()


# ── record_result ────────────────────────────────────────────────────────────


async def test_record_result_marks_done() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        updated = await dispatch.record_result(
            s, task_id=task.id, success=True, output="all good", error_message=None
        )
        await s.commit()
        assert updated is not None
        assert updated.status == "done"
        assert updated.output == "all good"
        assert updated.error_message is None


async def test_record_result_marks_failed() -> None:
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        updated = await dispatch.record_result(
            s, task_id=task.id, success=False, output="", error_message="boom"
        )
        await s.commit()
        assert updated is not None
        assert updated.status == "failed"
        assert updated.error_message == "boom"


async def test_record_result_unknown_task_is_none() -> None:
    async with memory_session() as s:
        result = await dispatch.record_result(
            s, task_id=uuid.uuid4(), success=True, output="", error_message=None
        )
        assert result is None


# ── await_completion ──────────────────────────────────────────────────────────


async def test_await_completion_returns_on_done_signal() -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        task_id = task.id

        async def _simulate_worker() -> None:
            # Give the awaiter time to subscribe, then write the result row +
            # publish the done channel (the worker's /result + publish path).
            await asyncio.sleep(0.05)
            await dispatch.record_result(
                s, task_id=task_id, success=True, output="done!", error_message=None
            )
            await s.commit()
            await redis.publish(
                dispatch.done_channel(task_id), json.dumps({"task_id": str(task_id)})
            )

        worker_task = asyncio.create_task(_simulate_worker())
        row = await dispatch.await_completion(redis, session=s, task_id=task_id, timeout_s=2.0)
        await worker_task
        assert row is not None
        assert row.status == "done"
        assert row.output == "done!"
    await redis.aclose()


async def test_await_completion_db_fallback_when_already_done() -> None:
    """If the result row is already terminal before subscribe, the DB fallback
    returns it even if no done message ever arrives."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        await dispatch.record_result(
            s, task_id=task.id, success=True, output="pre-done", error_message=None
        )
        await s.commit()
        row = await dispatch.await_completion(redis, session=s, task_id=task.id, timeout_s=0.3)
        assert row is not None
        assert row.status == "done"
        assert row.output == "pre-done"
    await redis.aclose()


async def test_await_completion_times_out_cleanly() -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        with pytest.raises(dispatch.TaskTimeout):
            await dispatch.await_completion(redis, session=s, task_id=task.id, timeout_s=0.2)
    await redis.aclose()


# ── ExecutorDispatchWorker single-tick ────────────────────────────────────────


async def _engine_session_factory() -> Any:
    """A fresh in-memory SQLite session factory the worker can poll."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.data import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine


async def test_dispatch_worker_tick_dispatches_pending_task() -> None:
    from backend.workers.executor_dispatch import ExecutorDispatchWorker

    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    sf, engine = await _engine_session_factory()

    worker_id: uuid.UUID
    task_id: uuid.UUID
    async with sf() as s:
        worker = await _seed_worker(
            s, workspace_id=workspace_id, capabilities=["claude_code"], heartbeat_age_s=5
        )
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        worker_id = worker.id
        task_id = task.id

    dispatch_worker = ExecutorDispatchWorker(session_factory=sf, redis=redis)
    processed = await dispatch_worker.dispatch_once()
    assert processed == 1

    async with sf() as s:
        refreshed = await s.get(ExecutorTaskRow, task_id)
        assert refreshed is not None
        assert refreshed.status == "dispatched"
        assert refreshed.worker_id == worker_id

    entries = await redis.xrange(dispatch.worker_stream(worker_id))
    assert len(entries) == 1
    await redis.aclose()
    await engine.dispose()


async def test_dispatch_worker_tick_leaves_pending_when_no_worker() -> None:
    from backend.workers.executor_dispatch import ExecutorDispatchWorker

    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    sf, engine = await _engine_session_factory()

    task_id: uuid.UUID
    async with sf() as s:
        # No worker seeded → nothing available.
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        task_id = task.id

    dispatch_worker = ExecutorDispatchWorker(session_factory=sf, redis=redis)
    processed = await dispatch_worker.dispatch_once()
    assert processed == 0

    async with sf() as s:
        refreshed = await s.get(ExecutorTaskRow, task_id)
        assert refreshed is not None
        # Stays pending — no crash, no dispatch.
        assert refreshed.status == "pending"
        assert refreshed.worker_id is None
    await redis.aclose()
    await engine.dispose()


async def test_dispatch_worker_start_stop_graceful() -> None:
    from backend.workers.executor_dispatch import ExecutorDispatchWorker

    redis = await _make_redis()
    sf, engine = await _engine_session_factory()
    dispatch_worker = ExecutorDispatchWorker(session_factory=sf, redis=redis)
    await dispatch_worker.start()
    await dispatch_worker.stop()
    await redis.aclose()
    await engine.dispose()
