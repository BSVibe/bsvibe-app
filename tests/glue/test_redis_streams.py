"""Redis Streams consumption mode — additive, config-gated worker trigger.

Workflow §12.5 #8 (Bundle G — Workers). DB-polling is the DEFAULT and stays
the tested path (``tests/glue/test_worker_runtime.py`` et al.). This module
proves the *opt-in* Redis Streams path:

* producer ``XADD`` (best-effort, soft-fail, gated on
  ``worker_mode="redis_streams"``) — the DB row stays the source of truth, the
  stream entry is only a notification;
* :class:`backend.workers.streams.RedisStreamConsumer` — XGROUP CREATE MKSTREAM
  → XREADGROUP → handler → XACK (at-least-once: un-acked redelivered);
* :func:`backend.workflow.infrastructure.workers.run.build_stream_consumers` — wires each worker's
  EXISTING single-tick handler (``drain_once`` / ``claim_once`` / ``_tick``) as
  the consumer handler, never duplicating business logic.

Redis client: ``fakeredis.aioredis`` when installed (CI + local dev), else a
real reachable Redis at ``settings.redis_url`` (CI provides ``redis:7`` at
``BSVIBE_REDIS_URL``). When neither is available the suite skips — mirroring
the Postgres-skip pattern in the sibling glue tests, but it ACTUALLY engages
whenever Redis (fake or real) is up so CI exercises the path.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from backend.config import Settings
from backend.workers.emit import STREAM_AGENT, STREAM_INTAKE, emit_stream_notification
from backend.workers.streams import RedisStreamConsumer

pytestmark = pytest.mark.asyncio


async def _seed_trigger_event(sf: Any, *, workspace_id: uuid.UUID) -> None:
    """Land one un-drained TriggerEvent the IntakeWorker will turn into a Request."""
    from datetime import UTC, datetime

    from backend.workflow.infrastructure.intake.db import TriggerEventRow, TriggerKind

    async with sf() as s:
        s.add(
            TriggerEventRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                product_id=None,
                source="direct",
                trigger_kind=TriggerKind.DIRECT,
                idempotency_key=uuid.uuid4().hex,
                payload={"text": "do the thing"},
                trace_id=None,
                received_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()


@pytest_asyncio.fixture
async def sf() -> Any:
    """In-memory SQLite session factory with every model table created."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.data import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


# --------------------------------------------------------------------------
# Redis client fixture — fakeredis preferred, real redis fallback, else skip
# --------------------------------------------------------------------------


async def _make_redis() -> Any:
    """Return a connected async redis client, or ``None`` if none reachable.

    Prefers ``fakeredis.aioredis`` (no service needed); falls back to a real
    redis at ``Settings().redis_url`` and pings it. ``decode_responses=True``
    so stream fields come back as ``str`` (matching the XADD field encoding).
    """
    try:
        import fakeredis
        import fakeredis.aioredis as fakeredis_aio

        # Isolated server per instance — the shared default server binds async
        # primitives to one event loop, breaking pytest-asyncio's per-test loops.
        return fakeredis_aio.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    except ImportError:
        pass

    try:
        import redis.asyncio as redis_aio

        client = redis_aio.from_url(Settings().redis_url, decode_responses=True)
        await client.ping()
        return client
    except Exception:
        return None


@pytest_asyncio.fixture
async def redis_client() -> Any:
    client = await _make_redis()
    if client is None:
        pytest.skip("no reachable Redis (install fakeredis or run a redis service)")
    # Isolate each test on its own key namespace via FLUSHDB.
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


# --------------------------------------------------------------------------
# Producer XADD → consumer XREADGROUP → handler → XACK
# --------------------------------------------------------------------------


async def test_producer_emit_then_consumer_reads_invokes_handler_and_acks(
    redis_client: Any,
) -> None:
    settings = Settings(worker_mode="redis_streams")
    ws = uuid.uuid4()

    # Producer side: the intake landing emits a notification (soft-fail, gated).
    emitted = await emit_stream_notification(
        redis_client,
        settings=settings,
        stream=STREAM_INTAKE,
        fields={"workspace_id": str(ws)},
    )
    assert emitted is True
    assert await redis_client.xlen(STREAM_INTAKE) == 1

    # Consumer side: the worker's existing single-tick handler is the handler.
    handled: list[dict[str, str]] = []

    async def handler(fields: dict[str, str]) -> None:
        handled.append(fields)

    consumer = RedisStreamConsumer(redis_client)
    processed = await consumer.consume_once(
        stream_name=STREAM_INTAKE,
        consumer_group="intake_worker",
        consumer_name="c1",
        handler=handler,
    )

    assert processed == 1
    assert handled == [{"workspace_id": str(ws)}]
    # XACKed → no pending entries for the group.
    pending = await redis_client.xpending(STREAM_INTAKE, "intake_worker")
    assert pending["pending"] == 0


# --------------------------------------------------------------------------
# At-least-once: a handler failure leaves the message un-acked → redelivered
# --------------------------------------------------------------------------


async def test_unacked_message_is_redelivered(redis_client: Any) -> None:
    settings = Settings(worker_mode="redis_streams")
    await emit_stream_notification(
        redis_client,
        settings=settings,
        stream=STREAM_AGENT,
        fields={"n": "1"},
    )

    consumer = RedisStreamConsumer(redis_client)

    # First pass: handler raises → message NOT acked (stays pending).
    async def failing(_fields: dict[str, str]) -> None:
        raise RuntimeError("boom")

    processed = await consumer.consume_once(
        stream_name=STREAM_AGENT,
        consumer_group="agent_worker",
        consumer_name="c1",
        handler=failing,
    )
    assert processed == 0
    pending = await redis_client.xpending(STREAM_AGENT, "agent_worker")
    assert pending["pending"] == 1

    # Second pass: reclaim the stalled (pending) entry and succeed → acked.
    handled: list[dict[str, str]] = []

    async def ok(fields: dict[str, str]) -> None:
        handled.append(fields)

    reprocessed = await consumer.consume_once(
        stream_name=STREAM_AGENT,
        consumer_group="agent_worker",
        consumer_name="c2",
        handler=ok,
        min_idle_ms=0,
    )
    assert reprocessed == 1
    assert handled == [{"n": "1"}]
    pending = await redis_client.xpending(STREAM_AGENT, "agent_worker")
    assert pending["pending"] == 0


# --------------------------------------------------------------------------
# Soft-fail: a Redis hiccup on emit must NOT raise (DB write already happened)
# --------------------------------------------------------------------------


async def test_emit_is_soft_fail_when_redis_raises() -> None:
    settings = Settings(worker_mode="redis_streams")

    class _BrokenRedis:
        async def xadd(self, *_args: Any, **_kwargs: Any) -> str:
            raise ConnectionError("redis down")

    emitted = await emit_stream_notification(
        _BrokenRedis(),
        settings=settings,
        stream=STREAM_INTAKE,
        fields={"x": "1"},
    )
    # No raise; returns False so the caller can log but the request path lives.
    assert emitted is False


async def test_emit_is_noop_when_db_polling_default() -> None:
    settings = Settings()  # worker_mode defaults to db_polling
    assert settings.worker_mode == "db_polling"

    calls: list[Any] = []

    class _RecordingRedis:
        async def xadd(self, *args: Any, **_kwargs: Any) -> str:
            calls.append(args)
            return "0-0"

    emitted = await emit_stream_notification(
        _RecordingRedis(),
        settings=settings,
        stream=STREAM_INTAKE,
        fields={"x": "1"},
    )
    # Gated off in DB-polling mode — never touches Redis.
    assert emitted is False
    assert calls == []


async def test_emit_is_noop_when_client_is_none() -> None:
    settings = Settings(worker_mode="redis_streams")
    emitted = await emit_stream_notification(
        None,
        settings=settings,
        stream=STREAM_INTAKE,
        fields={"x": "1"},
    )
    assert emitted is False


# --------------------------------------------------------------------------
# run.py wiring — Redis mode reuses each worker's EXISTING single-tick handler
# --------------------------------------------------------------------------


async def test_build_stream_consumers_reuses_existing_worker_handlers() -> None:
    """``build_stream_consumers`` maps each worker to a (stream, group, handler)
    binding whose handler is the worker's OWN single-tick method — proving the
    Redis path is just a different *trigger* for the same logic, not a rewrite.
    """
    from backend.workflow.infrastructure.workers.run import build_stream_consumers

    # Stub worker set exposing the real single-tick method names. Each returns
    # an awaitable count exactly like the production workers.
    async def _drain() -> int:
        return 1

    async def _claim() -> int:
        return 1

    async def _tick() -> int:
        return 1

    intake = SimpleNamespace(_name="intake_worker", drain_once=_drain)
    agent = SimpleNamespace(_name="agent_worker", _tick=_tick)
    delivery = SimpleNamespace(_name="delivery_worker", drain_once=_drain)
    settle = SimpleNamespace(_name="settle_worker", drain_once=_drain)
    relay = SimpleNamespace(_name="relay_worker", drain_once=_drain, claim_once=_claim)

    # relay is passed in but has NO stream binding — it must be omitted, not crash.
    bindings = build_stream_consumers([intake, agent, delivery, settle, relay])

    by_stream = {b.stream_name: b for b in bindings}
    # Each known worker maps to its stream; the consumer group is the worker name.
    assert set(by_stream) == {STREAM_INTAKE, STREAM_AGENT, "deliver", "settle"}
    assert by_stream[STREAM_INTAKE].consumer_group == "intake_worker"
    assert by_stream[STREAM_AGENT].consumer_group == "agent_worker"

    # The handler invokes the worker's own tick logic (does not duplicate it).
    for b in bindings:
        await b.handler({"trigger": "1"})

    # A worker with no stream binding (relay drains the audit outbox on its own
    # cadence, not driven by a producer event) is omitted, not crashed.
    assert "relay_worker" not in {b.consumer_group for b in bindings}


# --------------------------------------------------------------------------
# Producer wiring — IntakeWorker emits an ``agent`` notification per Request
# --------------------------------------------------------------------------


async def test_intake_worker_emits_agent_notification_in_redis_mode(
    redis_client: Any, sf: Any
) -> None:
    from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker

    ws = uuid.uuid4()
    await _seed_trigger_event(sf, workspace_id=ws)

    worker = IntakeWorker(
        session_factory=sf,
        redis_client=redis_client,
        settings=Settings(worker_mode="redis_streams"),
    )
    # The DB drain still produces the Request (source of truth) ...
    assert await worker.drain_once() == 1
    # ... AND a wake-up notification landed on the agent stream.
    assert await redis_client.xlen(STREAM_AGENT) == 1


async def test_intake_worker_does_not_emit_in_db_polling_default(
    redis_client: Any, sf: Any
) -> None:
    from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker

    ws = uuid.uuid4()
    await _seed_trigger_event(sf, workspace_id=ws)

    # Default mode + a client passed: gated OFF → no stream entry, drain still works.
    worker = IntakeWorker(session_factory=sf, redis_client=redis_client, settings=Settings())
    assert await worker.drain_once() == 1
    assert await redis_client.xlen(STREAM_AGENT) == 0


async def test_intake_worker_drain_survives_redis_emit_failure(sf: Any) -> None:
    from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker

    class _BrokenRedis:
        async def xadd(self, *_a: Any, **_k: Any) -> str:
            raise ConnectionError("redis down")

    ws = uuid.uuid4()
    await _seed_trigger_event(sf, workspace_id=ws)

    worker = IntakeWorker(
        session_factory=sf,
        redis_client=_BrokenRedis(),
        settings=Settings(worker_mode="redis_streams"),
    )
    # Redis emit fails, but the DB Request was committed → drain reports 1, no raise.
    assert await worker.drain_once() == 1
    async with sf() as s:
        from sqlalchemy import func, select

        from backend.workflow.infrastructure.intake.db import RequestRow

        total = (await s.execute(select(func.count()).select_from(RequestRow))).scalar_one()
        assert total == 1


# --------------------------------------------------------------------------
# Daemon orchestration — run_stream_consumers drives a worker tick via a stream
# --------------------------------------------------------------------------


async def test_run_stream_consumers_drives_worker_tick_from_a_stream(
    redis_client: Any,
) -> None:
    """End-to-end of the Redis-mode daemon: a producer XADD to the ``agent``
    stream causes the agent worker's tick handler to run, then the daemon stops
    cleanly. Proves the consumer loop wakes the EXISTING handler on a notification.
    """
    import asyncio

    from backend.workflow.infrastructure.workers.run import run_stream_consumers

    ticks = asyncio.Event()

    async def _tick() -> int:
        ticks.set()
        return 1

    # A worker exposing only ``_tick`` (the agent-worker shape) + the relay
    # worker shape (no stream binding → keeps polling, started/stopped cleanly).
    agent = SimpleNamespace(_name="agent_worker", _tick=_tick)

    started = asyncio.Event()
    stopped = asyncio.Event()

    class _RelayLike:
        _name = "relay_worker"

        async def start(self) -> None:
            started.set()

        async def stop(self) -> None:
            stopped.set()

    stop_event = asyncio.Event()
    daemon = asyncio.create_task(
        run_stream_consumers(
            workers=[agent, _RelayLike()],  # type: ignore[list-item]  # duck-typed
            redis_client=redis_client,
            stop_event=stop_event,
        )
    )

    # Produce a notification → the agent consumer should run its tick.
    await emit_stream_notification(
        redis_client,
        settings=Settings(worker_mode="redis_streams"),
        stream=STREAM_AGENT,
        fields={"workspace_id": str(uuid.uuid4())},
    )
    await asyncio.wait_for(ticks.wait(), timeout=5)
    assert started.is_set()  # the non-stream relay worker was started

    stop_event.set()
    await asyncio.wait_for(daemon, timeout=5)
    assert stopped.is_set()  # graceful shutdown stopped the poll worker too


async def test_build_worker_runtime_wires_redis_client_into_intake_in_redis_mode(
    redis_client: Any, sf: Any
) -> None:
    """The production runtime threads the redis client into the producer-side
    IntakeWorker (so it emits) when one is supplied; None keeps DB-polling clean.

    Execution + delivery deps are stubbed (the focus is the IntakeWorker
    producer wiring, not the gateway/KMS/delivery graph)."""
    from backend.workflow.infrastructure.workers import run as runtime
    from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker

    settings = Settings(worker_mode="redis_streams")
    execution = SimpleNamespace()  # AgentWorker only stores it; no method called here.
    delivery = SimpleNamespace()  # DeliveryWorker only stores the dispatcher.

    rt = runtime.build_worker_runtime(
        session_factory=sf,
        execution=execution,  # type: ignore[arg-type]
        delivery_adapter=delivery,  # type: ignore[arg-type]
        settings=settings,
        redis_client=redis_client,
    )
    intake = next(w for w in rt.workers if isinstance(w, IntakeWorker))
    assert intake._redis_client is redis_client
    assert intake._settings.worker_mode == "redis_streams"

    # No client + default mode → DB-polling IntakeWorker that never emits.
    rt_db = runtime.build_worker_runtime(
        session_factory=sf,
        execution=execution,  # type: ignore[arg-type]
        delivery_adapter=delivery,  # type: ignore[arg-type]
        settings=Settings(),
    )
    intake_db = next(w for w in rt_db.workers if isinstance(w, IntakeWorker))
    assert intake_db._redis_client is None
