"""HTTP-surface tests for the worker poll + result endpoints (Lift 2).

Exercises ``/api/v1/workers/poll`` + ``/api/v1/workers/result`` end-to-end over
an httpx ``ASGITransport``. Both are worker-token authed (``X-Worker-Token`` →
``get_current_worker``), NOT JWT. A ``fakeredis`` double backs the per-worker
Redis Stream the poll endpoint reads; the "worker process" is simulated by the
test directly (no real CLI / subprocess).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

# Register the executor tables on Base.metadata for db_engine.create_all.
import backend.executors.db  # noqa: F401
from backend.api.deps import get_db_session
from backend.api.main import create_app
from backend.api.v1 import workers as workers_router
from backend.executors import dispatch, service
from backend.executors.db import ExecutorTaskRow, WorkerRow

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _make_redis() -> Any:
    try:
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:  # pragma: no cover - declared dep
        pytest.skip("fakeredis not installed")
    client = fakeredis_aio.FakeRedis(decode_responses=True)
    await client.flushdb()
    return client


@pytest_asyncio.fixture
async def redis():
    client = await _make_redis()
    try:
        yield client
    finally:
        await client.aclose()


async def _seed_worker(db, *, capabilities: list[str]) -> tuple[uuid.UUID, str]:
    """Insert a worker row + return ``(id, plaintext_token)``."""
    token = uuid.uuid4().hex
    async with db() as s:
        worker = WorkerRow(
            workspace_id=uuid.uuid4(),
            name="w",
            labels=[],
            capabilities=list(capabilities),
            status="online",
            last_heartbeat=datetime.now(UTC),
            token_hash=service._hash_token(token),
            is_active=True,
        )
        s.add(worker)
        await s.commit()
        return worker.id, token


def _client(app, db, redis) -> httpx.AsyncClient:
    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[workers_router.get_poll_redis] = lambda: redis
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ── poll ──────────────────────────────────────────────────────────────────────


async def test_poll_returns_dispatched_messages_then_acks(db, redis) -> None:
    worker_id, token = await _seed_worker(db, capabilities=["claude_code"])

    # Dispatch a task onto the worker's stream (the producer side).
    async with db() as s:
        worker = await s.get(WorkerRow, worker_id)
        task = await dispatch.create_task(
            s, workspace_id=worker.workspace_id, executor_type="claude_code", prompt="hi"
        )
        await dispatch.dispatch_task(redis, session=s, task=task, worker_id=worker_id)
        await s.commit()

    app = create_app()
    async with _client(app, db, redis) as c:
        r = await c.post("/api/v1/workers/poll?count=5", headers={"X-Worker-Token": token})
        assert r.status_code == 200, r.text
        msgs = r.json()
        assert len(msgs) == 1
        assert msgs[0]["task_id"] == str(task.id)
        assert msgs[0]["executor_type"] == "claude_code"
        assert msgs[0]["action"] == "execute"

        # A second poll returns nothing — the first batch was auto-acked.
        r2 = await c.post("/api/v1/workers/poll", headers={"X-Worker-Token": token})
        assert r2.status_code == 200, r2.text
        assert r2.json() == []


async def test_poll_empty_stream_returns_empty(db, redis) -> None:
    _worker_id, token = await _seed_worker(db, capabilities=["claude_code"])
    app = create_app()
    async with _client(app, db, redis) as c:
        r = await c.post("/api/v1/workers/poll", headers={"X-Worker-Token": token})
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_poll_requires_worker_token(db, redis) -> None:
    app = create_app()
    async with _client(app, db, redis) as c:
        r = await c.post("/api/v1/workers/poll")
    assert r.status_code == 401, r.text


async def test_poll_bad_worker_token_is_401(db, redis) -> None:
    app = create_app()
    async with _client(app, db, redis) as c:
        r = await c.post("/api/v1/workers/poll", headers={"X-Worker-Token": "nope"})
    assert r.status_code == 401, r.text


# ── result ──────────────────────────────────────────────────────────────────


async def test_result_records_done(db, redis) -> None:
    worker_id, token = await _seed_worker(db, capabilities=["claude_code"])
    async with db() as s:
        worker = await s.get(WorkerRow, worker_id)
        task = await dispatch.create_task(
            s, workspace_id=worker.workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        task_id = task.id

    app = create_app()
    async with _client(app, db, redis) as c:
        r = await c.post(
            "/api/v1/workers/result",
            headers={"X-Worker-Token": token},
            json={"task_id": str(task_id), "success": True, "output": "ok", "error_message": None},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"

    async with db() as s:
        row = await s.get(ExecutorTaskRow, task_id)
        assert row is not None
        assert row.status == "done"
        assert row.output == "ok"


async def test_result_records_failed(db, redis) -> None:
    worker_id, token = await _seed_worker(db, capabilities=["claude_code"])
    async with db() as s:
        worker = await s.get(WorkerRow, worker_id)
        task = await dispatch.create_task(
            s, workspace_id=worker.workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        task_id = task.id

    app = create_app()
    async with _client(app, db, redis) as c:
        r = await c.post(
            "/api/v1/workers/result",
            headers={"X-Worker-Token": token},
            json={"task_id": str(task_id), "success": False, "output": "", "error_message": "boom"},
        )
        assert r.status_code == 200, r.text

    async with db() as s:
        row = await s.get(ExecutorTaskRow, task_id)
        assert row is not None
        assert row.status == "failed"
        assert row.error_message == "boom"


async def test_result_publishes_done_channel(db, redis) -> None:
    """The /result route publishes the done channel after recording the result,
    so an orchestrator awaiting on a backend-owned redis wakes promptly even
    though the remote worker reported over plain HTTP (no redis of its own)."""
    worker_id, token = await _seed_worker(db, capabilities=["claude_code"])
    async with db() as s:
        worker = await s.get(WorkerRow, worker_id)
        task = await dispatch.create_task(
            s, workspace_id=worker.workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.commit()
        task_id = task.id

    pubsub = redis.pubsub()
    await pubsub.subscribe(dispatch.done_channel(task_id))
    await pubsub.get_message(timeout=0.2)  # drain subscribe confirmation

    app = create_app()
    async with _client(app, db, redis) as c:
        r = await c.post(
            "/api/v1/workers/result",
            headers={"X-Worker-Token": token},
            json={"task_id": str(task_id), "success": True, "output": "ok", "error_message": None},
        )
        assert r.status_code == 200, r.text

    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
    assert msg is not None
    assert msg["type"] == "message"
    assert str(task_id) in msg["data"]
    await pubsub.unsubscribe(dispatch.done_channel(task_id))
    await pubsub.aclose()


async def test_result_requires_worker_token(db, redis) -> None:
    app = create_app()
    async with _client(app, db, redis) as c:
        r = await c.post(
            "/api/v1/workers/result",
            json={"task_id": str(uuid.uuid4()), "success": True},
        )
    assert r.status_code == 401, r.text


async def test_result_rejects_extra_fields(db, redis) -> None:
    _worker_id, token = await _seed_worker(db, capabilities=["claude_code"])
    app = create_app()
    async with _client(app, db, redis) as c:
        r = await c.post(
            "/api/v1/workers/result",
            headers={"X-Worker-Token": token},
            json={"task_id": str(uuid.uuid4()), "success": True, "bogus": 1},
        )
    assert r.status_code == 422, r.text
