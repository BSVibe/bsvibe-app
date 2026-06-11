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
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

# Importing the module db registers the tables on the shared Base.metadata so
# ``memory_session``'s create_all materialises them.
import backend.executors.db  # noqa: F401
from backend.executors import dispatch, service
from backend.executors.db import ExecutorTaskRow, WorkerRow

from .._support import memory_session, shared_file_sessionmaker

pytestmark = pytest.mark.asyncio


async def _make_redis() -> Any:
    """Return a connected fakeredis client, or skip when unavailable."""
    try:
        import fakeredis
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:  # pragma: no cover - fakeredis is a declared dep
        pytest.skip("fakeredis not installed")
    # Isolated server per instance — the shared default server binds async
    # primitives to one event loop, breaking pytest-asyncio's per-test loops.
    client = fakeredis_aio.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    await client.flushdb()
    return client


async def _seed_worker(
    s: Any,
    *,
    workspace_id: uuid.UUID,
    capabilities: list[str],
    status: str = "online",
    heartbeat_age_s: float | None = 0.0,
    last_in_flight: int | None = 0,
) -> WorkerRow:
    """Insert a worker row directly (bypassing the register flow)."""
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
        last_in_flight=last_in_flight,
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
        # B1: a task carries the run it belongs to (nullable for back-compat) and
        # starts with no artifact_refs.
        assert task.run_id is None
        assert task.artifact_refs is None


async def test_create_task_carries_run_id() -> None:
    """B1: ``create_task`` threads ``run_id`` onto the task row so the result
    path can resolve the run workspace to persist captured files into."""
    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s,
            workspace_id=workspace_id,
            executor_type="claude_code",
            prompt="p",
            run_id=run_id,
        )
        await s.commit()
        assert task.run_id == run_id


# ── Lift E21 — model routing ─────────────────────────────────────────────────


async def test_create_task_carries_model() -> None:
    """E21 — ``create_task`` threads ``model`` onto the task row so the worker
    can select an underlying LLM model (e.g. ``opencode-go/qwen3.6-plus``)
    instead of the executor CLI's plan-agent default."""
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s,
            workspace_id=workspace_id,
            executor_type="opencode",
            prompt="p",
            model="opencode-go/qwen3.6-plus",
        )
        await s.commit()
        assert task.model == "opencode-go/qwen3.6-plus"


async def test_create_task_model_defaults_to_none() -> None:
    """E21 — when no ``model`` is passed, the task row's ``model`` is NULL.
    Worker code interprets a missing/empty model as "use CLI default"."""
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s,
            workspace_id=workspace_id,
            executor_type="opencode",
            prompt="p",
        )
        await s.commit()
        assert task.model is None


async def test_dispatch_task_xadds_model_when_set() -> None:
    """E21 — when the task has a ``model`` set, ``dispatch_task`` includes it
    in the XADD payload so the worker can forward it to the executor."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        worker = await _seed_worker(
            s, workspace_id=workspace_id, capabilities=["opencode"], heartbeat_age_s=5
        )
        task = await dispatch.create_task(
            s,
            workspace_id=workspace_id,
            executor_type="opencode",
            prompt="hello",
            model="opencode-go/kimi-k2.6",
        )
        await s.commit()

        await dispatch.dispatch_task(redis, session=s, task=task, worker_id=worker.id)
        await s.commit()

        entries = await redis.xrange(dispatch.worker_stream(worker.id))
        assert len(entries) == 1
        _entry_id, fields = entries[0]
        assert fields["model"] == "opencode-go/kimi-k2.6"
        # Every stream field must remain a flat string.
        assert all(isinstance(v, str) for v in fields.values())
    await redis.aclose()


async def test_dispatch_task_omits_model_when_none() -> None:
    """E21 — when the task has no ``model`` set, ``dispatch_task`` MUST NOT
    include a ``model`` key in the XADD payload (or include it as the empty
    string). Redis Streams reject None — and the worker treats absence as
    "use CLI default" via ``task.get("model")``."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        worker = await _seed_worker(
            s, workspace_id=workspace_id, capabilities=["opencode"], heartbeat_age_s=5
        )
        task = await dispatch.create_task(
            s,
            workspace_id=workspace_id,
            executor_type="opencode",
            prompt="hello",
        )
        await s.commit()

        await dispatch.dispatch_task(redis, session=s, task=task, worker_id=worker.id)
        await s.commit()

        entries = await redis.xrange(dispatch.worker_stream(worker.id))
        assert len(entries) == 1
        _entry_id, fields = entries[0]
        # Either absent or empty string — never a Python ``None`` rendered as
        # the string "None", which would mis-route the worker.
        assert fields.get("model", "") == ""
        assert all(isinstance(v, str) for v in fields.values())
    await redis.aclose()


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


# ── Lift E16 — capacity-aware dispatch ───────────────────────────────────────


async def test_find_available_worker_excludes_saturated() -> None:
    """Lift E16 — a worker at ``last_in_flight >= cap`` is NOT selected.

    The worker's poll loop skips polling while at-cap, so a task XADDed
    onto its stream sits unread until a slot frees up. Pre-E16 the backend
    happily dispatched there and started a timer that expired before the
    worker even read the task. Now ``find_available_worker`` must exclude
    a saturated row entirely.
    """
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        await _seed_worker(
            s,
            workspace_id=workspace_id,
            capabilities=["opencode"],
            heartbeat_age_s=5,
            last_in_flight=3,
        )
        await s.commit()
        found = await dispatch.find_available_worker(
            s,
            workspace_id=workspace_id,
            executor_type="opencode",
            max_parallel_per_worker=3,
        )
        assert found is None


async def test_find_available_worker_picks_least_loaded() -> None:
    """Lift E16 — among free workers the lower-load one wins (round-robin via heartbeat).

    Two workers, both online + fresh + capability-matching, both below cap:
    the one with the older heartbeat (i.e. the one selection has not picked
    recently) wins. This is the same ``ORDER BY last_heartbeat ASC``
    semantics ``find_available_worker`` has always carried; the new
    capacity gate just composes with it instead of replacing it.
    """
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        older = await _seed_worker(
            s,
            workspace_id=workspace_id,
            capabilities=["opencode"],
            heartbeat_age_s=60,
            last_in_flight=2,
        )
        await _seed_worker(
            s,
            workspace_id=workspace_id,
            capabilities=["opencode"],
            heartbeat_age_s=5,
            last_in_flight=1,
        )
        await s.commit()
        found = await dispatch.find_available_worker(
            s,
            workspace_id=workspace_id,
            executor_type="opencode",
            max_parallel_per_worker=3,
        )
        assert found is not None
        assert found.id == older.id


async def test_find_available_worker_returns_none_when_all_saturated() -> None:
    """Lift E16 — every worker at-cap returns None (the chat call must wait)."""
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        await _seed_worker(
            s,
            workspace_id=workspace_id,
            capabilities=["opencode"],
            heartbeat_age_s=5,
            last_in_flight=3,
        )
        await _seed_worker(
            s,
            workspace_id=workspace_id,
            capabilities=["opencode"],
            heartbeat_age_s=5,
            last_in_flight=3,
        )
        await s.commit()
        found = await dispatch.find_available_worker(
            s,
            workspace_id=workspace_id,
            executor_type="opencode",
            max_parallel_per_worker=3,
        )
        assert found is None


async def test_find_available_worker_pre_e16_null_count_is_permitted() -> None:
    """Lift E16 — a NULL ``last_in_flight`` (pre-E16 worker shape) is allowed.

    Back-compat: rolling out E16 to the backend BEFORE upgrading workers
    must not capacity-exclude every legacy worker that never reports a
    count. NULL is treated as "no signal — let it through" so the
    workspace remains functional during the rollout.
    """
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        worker = await _seed_worker(
            s,
            workspace_id=workspace_id,
            capabilities=["opencode"],
            heartbeat_age_s=5,
            last_in_flight=None,
        )
        await s.commit()
        found = await dispatch.find_available_worker(
            s,
            workspace_id=workspace_id,
            executor_type="opencode",
            max_parallel_per_worker=3,
        )
        assert found is not None
        assert found.id == worker.id


async def test_find_available_worker_pinned_respected_even_when_saturated() -> None:
    """Lift E16 — a pinned worker_id is honoured even at-cap.

    The founder pinned a specific worker for a reason — usually a single
    dedicated machine. Capacity-excluding it would silently fall through
    to a different worker and break the pin invariant. The waiting
    behaviour belongs in :class:`ExecutorAdapter.chat`, not here.
    """
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        worker = await _seed_worker(
            s,
            workspace_id=workspace_id,
            capabilities=["opencode"],
            heartbeat_age_s=5,
            last_in_flight=3,
        )
        await s.commit()
        found = await dispatch.find_available_worker(
            s,
            workspace_id=workspace_id,
            executor_type="opencode",
            pinned_worker_id=worker.id,
            max_parallel_per_worker=3,
        )
        assert found is not None
        assert found.id == worker.id


async def test_find_available_worker_stale_heartbeat_excluded_with_busy_count() -> None:
    """Lift E16 / Part D — stale heartbeat trumps capacity reading.

    If a worker died mid-task without sending the final result, its
    ``last_in_flight`` would stay positive forever. The existing freshness
    check excludes the row entirely (it is not "available" anyway), so
    capacity-mode never sees it. Pins the interaction after the column
    addition — the count must not "leak" past the freshness gate.
    """
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        await _seed_worker(
            s,
            workspace_id=workspace_id,
            capabilities=["opencode"],
            heartbeat_age_s=300,  # stale (>HEARTBEAT_FRESHNESS_S=120)
            last_in_flight=3,
        )
        await s.commit()
        found = await dispatch.find_available_worker(
            s,
            workspace_id=workspace_id,
            executor_type="opencode",
            max_parallel_per_worker=3,
        )
        assert found is None


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


# ── cancel_task (Lift E14) ───────────────────────────────────────────────────


async def test_cancel_task_xadds_cancel_action_onto_worker_stream() -> None:
    """Lift E14 — backend cancels a task by XADDing an ``action=cancel`` message
    onto the worker's dedicated stream. The worker's next poll picks it up and
    terminates the running subprocess."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        worker = await _seed_worker(
            s, workspace_id=workspace_id, capabilities=["claude_code"], heartbeat_age_s=5
        )
        task_id = uuid.uuid4()

        await dispatch.cancel_task(redis, worker_id=worker.id, task_id=task_id)

        entries = await redis.xrange(dispatch.worker_stream(worker.id))
        assert len(entries) == 1
        _entry_id, fields = entries[0]
        assert fields["action"] == "cancel"
        assert fields["task_id"] == str(task_id)
        # Every stream field must be a flat string (Redis Streams constraint).
        assert all(isinstance(v, str) for v in fields.values())
    await redis.aclose()


async def test_cancel_task_swallows_redis_errors() -> None:
    """A cancel signal is best-effort — a redis hiccup must not cascade into
    the timeout-handling code path (which has already given up on the task)."""

    class _BoomRedis:
        async def xadd(self, *_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("redis down")

        async def publish(self, *_a: Any, **_kw: Any) -> Any:  # pragma: no cover — Protocol stub
            return None

        def pubsub(self) -> Any:  # pragma: no cover — Protocol stub
            raise NotImplementedError

    # Does not raise; logs warning + returns.
    await dispatch.cancel_task(_BoomRedis(), worker_id=uuid.uuid4(), task_id=uuid.uuid4())


# ── record_result ────────────────────────────────────────────────────────────


async def test_record_result_marks_done() -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        updated = await dispatch.record_result(
            s, redis, task_id=task.id, success=True, output="all good", error_message=None
        )
        await s.commit()
        assert updated is not None
        assert updated.status == "done"
        assert updated.output == "all good"
        assert updated.error_message is None
    await redis.aclose()


async def test_record_result_marks_failed() -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        updated = await dispatch.record_result(
            s, redis, task_id=task.id, success=False, output="", error_message="boom"
        )
        await s.commit()
        assert updated is not None
        assert updated.status == "failed"
        assert updated.error_message == "boom"
    await redis.aclose()


async def test_record_result_unknown_task_is_none() -> None:
    async with memory_session() as s:
        redis = await _make_redis()
        result = await dispatch.record_result(
            s, redis, task_id=uuid.uuid4(), success=True, output="", error_message=None
        )
        assert result is None
        await redis.aclose()


async def test_record_result_publishes_done_channel() -> None:
    """``record_result`` publishes the done channel so a remote worker (no redis
    of its own) still wakes an awaiter via the backend's redis client."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        task_id = task.id

        pubsub = redis.pubsub()
        await pubsub.subscribe(dispatch.done_channel(task_id))
        # Drain the subscribe confirmation so only the real publish remains.
        await pubsub.get_message(timeout=0.2)

        updated = await dispatch.record_result(
            s, redis, task_id=task_id, success=True, output="ok", error_message=None
        )
        await s.commit()
        assert updated is not None
        assert updated.status == "done"

        # The done channel carried a message identifying the completed task.
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        assert msg is not None
        assert msg["type"] == "message"
        assert str(task_id) in msg["data"]
        await pubsub.unsubscribe(dispatch.done_channel(task_id))
        await pubsub.aclose()
    await redis.aclose()


async def test_record_result_unknown_task_does_not_publish() -> None:
    """An unknown task id is a no-op — no row, no publish."""
    redis = await _make_redis()
    task_id = uuid.uuid4()
    pubsub = redis.pubsub()
    await pubsub.subscribe(dispatch.done_channel(task_id))
    await pubsub.get_message(timeout=0.2)

    async with memory_session() as s:
        result = await dispatch.record_result(
            s, redis, task_id=task_id, success=True, output="", error_message=None
        )
        assert result is None

    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.3)
    assert msg is None
    await pubsub.unsubscribe(dispatch.done_channel(task_id))
    await pubsub.aclose()
    await redis.aclose()


# ── record_result: file persistence (B1) ─────────────────────────────────────


async def test_record_result_persists_files_and_records_refs(tmp_path: Any) -> None:
    """B1: returned worker files are written under ``run_workspace_root/<run_id>/``
    and their relative paths recorded on ``task.artifact_refs``."""
    import base64

    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p", run_id=run_id
        )
        await s.commit()
        task_id = task.id

        files = [
            {
                "path": "out.txt",
                "content_b64": base64.b64encode(b"hello").decode(),
                "truncated": False,
            },
            {
                "path": "src/app.py",
                "content_b64": base64.b64encode(b"x = 1\n").decode(),
                "truncated": False,
            },
        ]
        updated = await dispatch.record_result(
            s,
            redis,
            task_id=task_id,
            success=True,
            output="ok",
            error_message=None,
            files=files,
            run_workspace_root=str(tmp_path),
        )
        await s.commit()
        assert updated is not None
        assert updated.status == "done"
        # Files landed under run dir.
        run_dir = tmp_path / str(run_id)
        assert (run_dir / "out.txt").read_bytes() == b"hello"
        assert (run_dir / "src" / "app.py").read_bytes() == b"x = 1\n"
        # Refs recorded (order-independent).
        assert set(updated.artifact_refs or []) == {"out.txt", "src/app.py"}
    await redis.aclose()


async def test_record_result_rejects_traversal_paths(tmp_path: Any) -> None:
    """B1: a ``../escape.txt`` file path must NOT be written outside the run dir,
    and must NOT be recorded as a ref."""
    import base64

    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p", run_id=run_id
        )
        await s.commit()
        files = [
            {
                "path": "good.txt",
                "content_b64": base64.b64encode(b"ok").decode(),
                "truncated": False,
            },
            {
                "path": "../escape.txt",
                "content_b64": base64.b64encode(b"pwned").decode(),
                "truncated": False,
            },
        ]
        updated = await dispatch.record_result(
            s,
            redis,
            task_id=task.id,
            success=True,
            output="ok",
            error_message=None,
            files=files,
            run_workspace_root=str(tmp_path),
        )
        await s.commit()
        assert updated is not None
        # The traversal target was NOT written.
        assert not (tmp_path / "escape.txt").exists()
        # Only the safe ref recorded.
        assert updated.artifact_refs == ["good.txt"]
    await redis.aclose()


async def test_record_result_skips_files_when_no_run_id(tmp_path: Any) -> None:
    """B1 back-compat: a task with ``run_id is None`` skips persistence entirely
    (no run dir to anchor paths to)."""
    import base64

    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        files = [
            {"path": "out.txt", "content_b64": base64.b64encode(b"hi").decode(), "truncated": False}
        ]
        updated = await dispatch.record_result(
            s,
            redis,
            task_id=task.id,
            success=True,
            output="ok",
            error_message=None,
            files=files,
            run_workspace_root=str(tmp_path),
        )
        await s.commit()
        assert updated is not None
        assert updated.status == "done"
        # Nothing persisted; no refs.
        assert not any(tmp_path.iterdir())
        assert not (updated.artifact_refs or [])
    await redis.aclose()


# ── await_completion ──────────────────────────────────────────────────────────


async def test_await_completion_returns_on_done_signal() -> None:
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    # The awaiter and the worker run CONCURRENTLY, so they must use SEPARATE
    # sessions (an AsyncSession is not concurrency-safe — sharing one collides
    # with "session is in 'prepared' state" when the poll's read races the
    # worker's commit). A file-WAL engine gives each session its own connection.
    async with shared_file_sessionmaker() as sf:
        async with sf() as setup_s:
            task = await dispatch.create_task(
                setup_s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
            )
            await setup_s.commit()
            task_id = task.id

        async def _simulate_worker() -> None:
            # Give the awaiter time to subscribe, then write the result row on
            # its OWN session. ``record_result`` publishes the done channel
            # (the backend's /result path — a remote worker can't publish).
            await asyncio.sleep(0.05)
            async with sf() as worker_s:
                await dispatch.record_result(
                    worker_s,
                    redis,
                    task_id=task_id,
                    success=True,
                    output="done!",
                    error_message=None,
                )
                await worker_s.commit()

        async with sf() as awaiter_s:
            worker_task = asyncio.create_task(_simulate_worker())
            row = await dispatch.await_completion(
                redis, session=awaiter_s, task_id=task_id, timeout_s=2.0
            )
            await worker_task
        assert row is not None
        assert row.status == "done"
        assert row.output == "done!"
    await redis.aclose()


async def test_await_completion_db_poll_resolves_without_signal() -> None:
    """Belt-and-braces: even when NO done signal is ever published, the periodic
    DB poll resolves the awaiter soon after the row becomes terminal — well
    before ``timeout_s``, not at the deadline."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    # Concurrent awaiter + worker → separate sessions on a shared file-WAL DB.
    async with shared_file_sessionmaker() as sf:
        async with sf() as setup_s:
            task = await dispatch.create_task(
                setup_s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
            )
            await setup_s.commit()
            task_id = task.id

        async def _silent_worker() -> None:
            # Mark the row terminal but DO NOT publish — simulates a remote
            # worker whose backend somehow missed the publish entirely.
            await asyncio.sleep(0.05)
            async with sf() as worker_s:
                t = await worker_s.get(ExecutorTaskRow, task_id)
                assert t is not None
                t.status = "done"
                t.output = "silent"
                await worker_s.commit()

        async with sf() as awaiter_s:
            worker_task = asyncio.create_task(_silent_worker())
            loop = asyncio.get_event_loop()
            started = loop.time()
            timeout_s = 30.0
            row = await dispatch.await_completion(
                redis, session=awaiter_s, task_id=task_id, timeout_s=timeout_s
            )
            elapsed = loop.time() - started
            await worker_task
        assert row is not None
        assert row.status == "done"
        assert row.output == "silent"
        # Resolved via the poll safety net, NOT by burning the full timeout.
        assert elapsed < timeout_s / 2
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
            s, redis, task_id=task.id, success=True, output="pre-done", error_message=None
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


# B14: ``ExecutorDispatchWorker`` + ``claim_pending_task`` were deleted as dead
# code — the orphan alt-dispatch design was never wired into
# :func:`build_worker_runtime`; real executor dispatch lives inline in
# :class:`backend.executors.orchestrator.ExecutorOrchestrator`. The
# corresponding tests are removed (see ``tests/glue/test_b14_cleanup_liveness.py``
# for the deletion assertions).
