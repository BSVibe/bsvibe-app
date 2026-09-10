"""Worker dispatch streams must be bounded — 감사 §Ⅱ "Redis 스트림 XTRIM 0건".

``tasks:worker:{id}`` is a NOTIFICATION queue, not a log: the ``executor_tasks``
row is the source of truth and the stream only tells a worker "there is
something for you". The worker drains it with XREADGROUP + auto-ack — but a
Redis consumer group does NOT delete an acked entry, and nothing ever called
XTRIM, so every dispatch that ever happened stayed resident forever.

Measured on prod (2026-09-10): 7 streams, 6,289 entries, **57.6 MB of a 66 MB
Redis** — 87%. One stale worker's stream alone held 40 MB. Five of the seven
belonged to workers that no longer exist, because ``revoke_worker`` never
touched Redis; they held 9.7 MB that nothing would ever free.

Two independent defects, so two fixes:

1. **Bound every stream at the XADD.** ``maxlen`` + ``approximate`` trims lazily
   at node boundaries (near-zero cost) and caps a stream no matter who owns it.
   This is the load-bearing one: it bounds orphans too.
2. **Free the key when a worker is deliberately removed.** ``revoke_worker``
   deletes the stream, best-effort — the DB row is truth, so a Redis hiccup must
   never fail a revoke.

**Why trimming cannot drop a task.** Dispatch is capacity-gated
(``max_parallel_tasks_per_worker``, default 3) and the worker polls every 5s, so
a live stream's UNDELIVERED backlog is a handful of entries. Losing one would
take the cap's worth of dispatches inside a single poll gap. The cap is a
setting so an operator can raise it, and the test below pins that a capped
stream still delivers its newest entry.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

# Importing the module db registers the tables on the shared Base.metadata so
# ``memory_session``'s create_all materialises them.
import backend.executors.db  # noqa: F401
from backend.executors import dispatch, service
from backend.executors.db import WorkerRow

from .._support import memory_session


async def _make_redis() -> Any:
    """Return a connected fakeredis client, or skip when unavailable."""
    try:
        import fakeredis.aioredis as fakeredis_aio  # noqa: PLC0415
    except ImportError:  # pragma: no cover - fakeredis is a declared test dep
        pytest.skip("fakeredis not installed")
    return fakeredis_aio.FakeRedis(decode_responses=True)


class _RecordingRedis:
    """Captures the kwargs each XADD was called with."""

    def __init__(self) -> None:
        self.xadds: list[tuple[str, dict[str, Any]]] = []

    async def xadd(self, name: str, fields: dict[str, Any], **kwargs: Any) -> Any:
        self.xadds.append((name, kwargs))
        return "1-0"

    async def delete(self, *names: str) -> int:
        return len(names)


# ── Fix 1: every XADD is bounded ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_xadd_caps_the_stream() -> None:
    """The dispatch XADD passes a bound. Unbounded is what filled prod's Redis."""
    redis = _RecordingRedis()
    workspace_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.flush()
        await dispatch.dispatch_task(redis, session=s, task=task, worker_id=worker_id)

    assert len(redis.xadds) == 1
    _name, kwargs = redis.xadds[0]
    assert kwargs.get("maxlen") == dispatch.WORKER_STREAM_MAXLEN
    # Approximate trimming is what makes the cap free: exact MAXLEN forces Redis
    # to walk the stream on every XADD.
    assert kwargs.get("approximate") is True


@pytest.mark.asyncio
async def test_cancel_xadd_caps_the_stream_too() -> None:
    """The OTHER XADD site. A cap on one writer leaves the stream unbounded."""
    redis = _RecordingRedis()
    await dispatch.cancel_task(redis, worker_id=uuid.uuid4(), task_id=uuid.uuid4())

    assert len(redis.xadds) == 1
    _name, kwargs = redis.xadds[0]
    assert kwargs.get("maxlen") == dispatch.WORKER_STREAM_MAXLEN
    assert kwargs.get("approximate") is True


@pytest.mark.asyncio
async def test_the_stream_actually_stays_bounded_under_real_redis(monkeypatch) -> None:
    """The proposition, end to end: dispatch through the REAL code path many
    times against a real stream implementation and the stream stays bounded.

    Asserting the kwarg proves we asked; this proves Redis obeyed AND that the
    ask is wired into the path production uses. The cap is monkeypatched small
    so the test does not have to write the production default's worth of
    entries. ``approximate`` trims at node boundaries, so the length settles
    NEAR the cap — the assertion allows that slack while still failing loudly
    for an unbounded stream, which would hold all 60.
    """
    monkeypatch.setattr(dispatch, "WORKER_STREAM_MAXLEN", 5)
    redis = await _make_redis()
    workspace_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    stream = dispatch.worker_stream(worker_id)

    async with memory_session() as s:
        for _ in range(60):
            task = await dispatch.create_task(
                s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
            )
            await s.flush()
            await dispatch.dispatch_task(redis, session=s, task=task, worker_id=worker_id)

    length = await redis.xlen(stream)
    assert length < 60, "an unbounded stream keeps every dispatch that ever happened"


@pytest.mark.asyncio
async def test_a_capped_stream_still_delivers_its_newest_entry() -> None:
    """Trimming must drop OLD notifications, never the one just dispatched.

    The cap exists to bound a log nobody reads twice; if it could evict the
    entry a worker has not polled yet, it would trade a disk problem for a lost
    run. Newest-survives is the property that makes the cap safe.
    """
    redis = await _make_redis()
    worker_id = uuid.uuid4()
    stream = dispatch.worker_stream(worker_id)

    for i in range(50):
        await redis.xadd(stream, {"task_id": str(i)}, maxlen=10, approximate=True)

    entries = await redis.xrange(stream, count=1000)
    newest_fields = entries[-1][1]
    assert newest_fields["task_id"] == "49"


# ── Fix 2: a revoked worker's stream is freed ────────────────────────────────


async def _register(s, workspace_id: uuid.UUID) -> WorkerRow:
    row, _token = await service.register_worker_for_workspace(
        s, workspace_id=workspace_id, name="w", labels=[], capabilities=["claude_code"]
    )
    await s.flush()
    return row


@pytest.mark.asyncio
async def test_revoke_deletes_the_workers_stream() -> None:
    """Prod held five streams for workers that no longer existed (9.7 MB).

    ``revoke_worker`` never touched Redis, so the key outlived the worker with
    nothing left that would ever look at it again.
    """
    redis = await _make_redis()
    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        row = await _register(s, workspace_id)
        stream = dispatch.worker_stream(row.id)
        await redis.xadd(stream, {"task_id": "1"})
        assert await redis.exists(stream) == 1

        revoked = await service.revoke_worker(
            s, workspace_id=workspace_id, worker_id=row.id, redis=redis
        )
        assert revoked is not None
        assert await redis.exists(stream) == 0


@pytest.mark.asyncio
async def test_revoke_survives_a_dead_redis() -> None:
    """The DB row is truth; stream cleanup is a courtesy.

    A revoke that 500s because Redis blipped would leave the founder unable to
    remove a worker they no longer trust — strictly worse than a leaked key.
    """

    class _BoomRedis:
        async def delete(self, *_names: str) -> int:
            raise RuntimeError("redis is down")

    workspace_id = uuid.uuid4()
    async with memory_session() as s:
        row = await _register(s, workspace_id)
        revoked = await service.revoke_worker(
            s, workspace_id=workspace_id, worker_id=row.id, redis=_BoomRedis()
        )
        assert revoked is not None
        assert revoked.is_active is False


@pytest.mark.asyncio
async def test_revoke_of_a_foreign_worker_touches_no_stream() -> None:
    """A cross-workspace revoke is a no-op — it must not delete the real owner's
    stream on the way to returning ``None``."""
    redis = await _make_redis()
    owner_ws = uuid.uuid4()
    async with memory_session() as s:
        row = await _register(s, owner_ws)
        stream = dispatch.worker_stream(row.id)
        await redis.xadd(stream, {"task_id": "1"})

        result = await service.revoke_worker(
            s, workspace_id=uuid.uuid4(), worker_id=row.id, redis=redis
        )
        assert result is None
        assert await redis.exists(stream) == 1
