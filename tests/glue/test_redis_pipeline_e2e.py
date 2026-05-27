"""Redis Streams pipeline end-to-end — the FULL Direct path flows via Redis.

This is the proof that ``worker_mode="redis_streams"`` is functional end to
end (Workflow §11.1 / §12.5 #8). PR #53 wired the consumer side + one producer
(IntakeWorker → ``agent``); this proves every remaining producer XADD site is
wired so a founder POST drives the whole chain through Redis notifications:

    POST /api/v1/messages
      → DirectTrigger.submit                → TriggerEventRow (committed)
      → producer XADD ``intake`` stream     ← messages.py
      → IntakeWorker.drain_once (consumer)  → RequestRow (OPEN)
      → producer XADD ``agent`` stream      ← IntakeWorker (PR #53)
      → AgentWorker._tick (consumer)        → ExecutionRun → verified
      → producer XADD ``deliver`` + ``settle`` streams ← RunOrchestrator
      → DeliveryWorker.drain_once (consumer)→ dispatched to the sink

We step the consumers one stream at a time (``consume_once`` per binding) so
the test asserts *each producer emitted* + *each consumer advanced the chain* —
proving the pipeline is functional via Redis, not via single-tick shortcuts.

The work LLM is the deterministic ``_ScriptedLlm``; the sandbox is the host
``NoopSandboxManager``; the delivery sink is an in-test ``PluginDispatchAdapter``
— the same doubles as ``tests/glue/test_direct_path_e2e.py``.

Redis client: ``fakeredis.aioredis`` when installed (CI + local dev), else a
real reachable Redis at ``settings.redis_url`` (mirrors PR #53's
``tests/glue/test_redis_streams.py``). Skips only when neither is available.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.config import Settings
from backend.delivery.db import DeliveryEventRow
from backend.delivery.schema import ActionResult, DeliveryResult
from backend.execution.db import Deliverable, ExecutionRun, RunStatus
from backend.execution.orchestrator import LoopToolCall, LoopTurn, RunOrchestrator
from backend.intake.db import RequestRow, RequestStatus, TriggerEventRow
from backend.skills.loader import SkillLoader
from backend.supervisor.sandbox import NoopSandboxManager
from backend.workers import emit as emit_mod
from backend.workers.agent_worker import AgentExecutionDeps, AgentWorker
from backend.workers.delivery_worker import DeliveryWorker, DeliveryWorkerConfig
from backend.workers.emit import (
    STREAM_AGENT,
    STREAM_DELIVER,
    STREAM_INTAKE,
    STREAM_SETTLE,
    emit_stream_notification,
)
from backend.workers.intake_worker import IntakeWorker
from backend.workers.streams import RedisStreamConsumer

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

_REDIS_SETTINGS = Settings(worker_mode="redis_streams")


# --------------------------------------------------------------------------
# Fixtures — session factory, redis client (fakeredis preferred), HTTP client
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sf() -> Any:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _make_redis() -> Any:
    """Return a connected async redis client, or ``None`` if none reachable."""
    try:
        import fakeredis.aioredis as fakeredis_aio

        return fakeredis_aio.FakeRedis(decode_responses=True)
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
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


# --------------------------------------------------------------------------
# Test doubles (mirror tests/glue/test_direct_path_e2e.py)
# --------------------------------------------------------------------------


class _ScriptedLlm:
    """A deterministic ``LoopLlm`` — pops the next pre-programmed turn FIFO."""

    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        if not self._turns:
            raise AssertionError("ScriptedLlm exhausted — loop requested an unscripted turn")
        return self._turns.pop(0)


class _SinkDispatcher:
    """An in-test ``PluginDispatchAdapter`` — records what was dispatched."""

    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    async def dispatch(self, **kwargs: Any) -> DeliveryResult:
        self.dispatched.append(kwargs)
        return DeliveryResult(
            workspace_id=kwargs["workspace_id"],
            deliverable_id=kwargs["deliverable_id"],
            artifact_type=kwargs["artifact_type"],
            actions=[ActionResult(action="sink", succeeded=True)],
            delivered_at=datetime.now(tz=UTC),
        )


def _tc(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


def _scripted_verified_run() -> _ScriptedLlm:
    return _ScriptedLlm(
        [
            LoopTurn(
                content="Writing the deliverable and declaring how to check it.",
                tool_calls=(
                    _tc(
                        "declare_verification",
                        checks=[{"kind": "command", "command": "test -f answer.txt"}],
                    ),
                    _tc("file_write", path="answer.txt", content="42\n"),
                ),
            ),
            LoopTurn(content="Done — answer.txt written.", tool_calls=()),
        ]
    )


def _execution_deps(
    workspace_root: Path, *, redis_client: Any, settings: Settings
) -> AgentExecutionDeps:
    """Production-shaped deps but with the scripted LLM + Noop sandbox.

    Crucially the orchestrator is built WITH the redis client + redis-mode
    settings (exactly as ``backend.workers.run.build_agent_execution_deps``
    threads them in redis mode) so the verified terminal emits the
    ``deliver`` + ``settle`` notifications."""
    llm = _scripted_verified_run()

    def _skill_loader_for(ws_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(workspace_root / "skills" / str(ws_id))
        loader.load_all()
        return loader

    return AgentExecutionDeps(
        skill_loader_for=_skill_loader_for,
        orchestrator_factory=lambda session, _run: RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            redis_client=redis_client,
            settings=settings,
        ),
        workspace_root=workspace_root,
    )


@pytest_asyncio.fixture
async def client(sf: Any, founder_id: uuid.UUID, workspace_id: uuid.UUID, redis_client: Any) -> Any:
    """HTTP client whose /api/v1/messages route emits onto ``redis_client``.

    The route acquires its emit client from the process-wide cache
    (``get_emit_redis_client``); we pre-seed that cache with the test's
    fakeredis so the produced ``intake`` XADD lands on the SAME stream the test
    consumer drains. ``worker_mode`` is forced to redis via a settings override
    in ``backend.config.get_settings``'s lru_cache — instead we monkeypatch the
    module-level cache the route reads, which is cleaner than mutating env."""
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session() -> Any:
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def seeded_product(
    sf: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID
) -> uuid.UUID:
    """L-P1: messages API requires a workspace to have at least one product."""
    from backend.workspaces.db import ProductRow

    product_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name="test-product",
                slug="test-product",
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return product_id


# --------------------------------------------------------------------------
# Helpers — drain one stream's notifications into the worker's tick handler
# --------------------------------------------------------------------------


async def _drain_stream(
    consumer: RedisStreamConsumer, *, stream: str, group: str, tick: Any
) -> int:
    """Consume every NEW entry on ``stream`` and run the worker's tick per entry.

    Returns the number of stream entries the consumer processed + acked. The
    handler ignores the notification fields (the worker reads its own DB source
    of truth) — exactly the ``backend.workers.run._tick_handler`` shape."""

    async def _handler(_fields: dict[str, Any]) -> None:
        await tick()

    return await consumer.consume_once(
        stream_name=stream, consumer_group=group, consumer_name="c1", handler=_handler
    )


# --------------------------------------------------------------------------
# The full Redis-driven pipeline
# --------------------------------------------------------------------------


async def test_full_direct_path_flows_via_redis(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    redis_client: Any,
    workspace_id: uuid.UUID,
    seeded_product: uuid.UUID,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The route reads worker_mode + the emit client from get_settings() +
    # get_emit_redis_client(). Force redis mode + bind the test's fakeredis as
    # the process-wide emit client so the route's intake XADD lands here.
    monkeypatch.setattr("backend.api.v1.messages.get_settings", lambda: _REDIS_SETTINGS)
    monkeypatch.setattr(
        "backend.api.v1.messages.get_emit_redis_client", lambda _settings: redis_client
    )

    consumer = RedisStreamConsumer(redis_client)

    # 1. Founder POSTs → TriggerEvent committed → producer XADD on ``intake``.
    resp = await client.post("/api/v1/messages", json={"text": "build the answer file"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["duplicate"] is False

    async with sf() as s:
        triggers = (await s.execute(select(TriggerEventRow))).scalars().all()
    assert len(triggers) == 1 and triggers[0].source == "direct"
    # The producer emitted onto the intake stream (the pipeline's step-1 wake-up).
    assert await redis_client.xlen(STREAM_INTAKE) == 1

    # 2. Drain ``intake`` → IntakeWorker.drain_once → Request (OPEN) + ``agent`` XADD.
    intake = IntakeWorker(session_factory=sf, redis_client=redis_client, settings=_REDIS_SETTINGS)
    assert (
        await _drain_stream(
            consumer, stream=STREAM_INTAKE, group="intake_worker", tick=intake.drain_once
        )
        == 1
    )
    async with sf() as s:
        requests = (await s.execute(select(RequestRow))).scalars().all()
    assert len(requests) == 1 and requests[0].status is RequestStatus.OPEN
    # IntakeWorker (PR #53) emitted the ``agent`` wake-up for the new Request.
    assert await redis_client.xlen(STREAM_AGENT) == 1

    # 3. Drain ``agent`` → AgentWorker._tick (claim + drive) → verified run +
    #    Deliverable + DeliveryEvent + producer XADD on ``deliver`` AND ``settle``.
    deps = _execution_deps(tmp_path, redis_client=redis_client, settings=_REDIS_SETTINGS)
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert (
        await _drain_stream(consumer, stream=STREAM_AGENT, group="agent_worker", tick=agent._tick)
        >= 1
    )
    async with sf() as s:
        run = (await s.execute(select(ExecutionRun))).scalar_one()
        assert run.status is RunStatus.REVIEW_READY
        run_id = run.id
        deliverable = (await s.execute(select(Deliverable))).scalar_one()
        deliverable_id = deliverable.id
        deliver_event = (await s.execute(select(DeliveryEventRow))).scalar_one()
        assert deliver_event.deliverable_id == deliverable_id
    # The work LLM wrote the artifact into the run workspace.
    assert (tmp_path / str(run_id) / "answer.txt").read_text() == "42\n"
    # The orchestrator emitted BOTH terminal notifications.
    assert await redis_client.xlen(STREAM_DELIVER) == 1
    assert await redis_client.xlen(STREAM_SETTLE) == 1

    # 4. Drain ``deliver`` → DeliveryWorker.drain_once → dispatched to the sink.
    sink = _SinkDispatcher()
    delivery = DeliveryWorker(
        session_factory=sf,
        dispatcher=sink,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert (
        await _drain_stream(
            consumer, stream=STREAM_DELIVER, group="delivery_worker", tick=delivery.drain_once
        )
        == 1
    )
    assert len(sink.dispatched) == 1
    assert sink.dispatched[0]["deliverable_id"] == deliverable_id
    assert sink.dispatched[0]["workspace_id"] == workspace_id
    # Delivery event drained off the queue (delivered end to end via Redis).
    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None


# --------------------------------------------------------------------------
# Gated: db_polling (default) producers are pure no-ops — no Redis touched
# --------------------------------------------------------------------------


async def test_messages_producer_is_noop_in_db_polling(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    seeded_product: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the default db_polling mode the route emits nothing — and crucially
    never even acquires a Redis client (get_emit_redis_client returns None
    without building one)."""
    acquired: list[Any] = []
    real_get = emit_mod.get_emit_redis_client

    def _spy(settings: Settings) -> Any:
        c = real_get(settings)
        acquired.append(c)
        return c

    # Default settings (worker_mode == "db_polling").
    monkeypatch.setattr("backend.api.v1.messages.get_settings", Settings)
    monkeypatch.setattr("backend.api.v1.messages.get_emit_redis_client", _spy)

    resp = await client.post("/api/v1/messages", json={"text": "no redis please"})
    assert resp.status_code == 202

    # The DB write happened (source of truth) regardless of Redis.
    async with sf() as s:
        total = (await s.execute(select(func.count()).select_from(TriggerEventRow))).scalar_one()
    assert total == 1
    # The client acquisition returned None in db_polling — no Redis built/touched.
    assert acquired == [None]


async def test_get_emit_redis_client_builds_nothing_in_db_polling() -> None:
    """Unit guard: the lazy acquirer never imports/constructs a client in the
    default mode (the gate is the FIRST thing checked)."""
    emit_mod.reset_emit_redis_client()
    assert emit_mod.get_emit_redis_client(Settings()) is None
    # And the cache stays empty.
    assert emit_mod._EMIT_CLIENT_CACHE[0] is None


# --------------------------------------------------------------------------
# Soft-fail: a producer emit with Redis DOWN → the request/run still succeeds
# --------------------------------------------------------------------------


async def test_messages_post_succeeds_when_redis_down(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    seeded_product: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken Redis on the intake producer must not break the POST — the
    TriggerEvent is committed (source of truth) and the 202 still returns."""

    class _BrokenRedis:
        async def xadd(self, *_a: Any, **_k: Any) -> str:
            raise ConnectionError("redis down")

    monkeypatch.setattr("backend.api.v1.messages.get_settings", lambda: _REDIS_SETTINGS)
    monkeypatch.setattr("backend.api.v1.messages.get_emit_redis_client", lambda _s: _BrokenRedis())

    resp = await client.post("/api/v1/messages", json={"text": "redis is down"})
    assert resp.status_code == 202, resp.text
    async with sf() as s:
        total = (await s.execute(select(func.count()).select_from(TriggerEventRow))).scalar_one()
    assert total == 1


async def test_orchestrator_verified_survives_redis_emit_failure(
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    """A broken Redis on the deliver/settle producer must not revert the
    verified terminal — the run reaches REVIEW_READY + the Deliverable +
    DeliveryEvent + settle activity are all written regardless."""
    from backend.intake.db import TriggerKind

    class _BrokenRedis:
        async def xadd(self, *_a: Any, **_k: Any) -> str:
            raise ConnectionError("redis down")

    # Seed a Request directly (skip the intake hop) so the agent drives a run.
    async with sf() as s:
        trig = TriggerEventRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=None,
            source="direct",
            trigger_kind=TriggerKind.DIRECT,
            idempotency_key=uuid.uuid4().hex,
            payload={"text": "build the answer file"},
            trace_id=None,
            received_at=datetime.now(tz=UTC),
        )
        s.add(trig)
        await s.commit()

    intake = IntakeWorker(session_factory=sf)
    assert await intake.drain_once() == 1

    deps = _execution_deps(tmp_path, redis_client=_BrokenRedis(), settings=_REDIS_SETTINGS)
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.claim_once() == 1
    # drive_once must NOT raise even though the deliver/settle emit fails.
    assert await agent.drive_once() == 1

    async with sf() as s:
        run = (await s.execute(select(ExecutionRun))).scalar_one()
        assert run.status is RunStatus.REVIEW_READY
        assert (await s.execute(select(Deliverable))).scalar_one() is not None
        assert (await s.execute(select(DeliveryEventRow))).scalar_one() is not None


async def test_emit_deliver_settle_no_op_without_client() -> None:
    """The orchestrator's terminal emit is a no-op when no client is injected
    (the default for every existing caller) — gated off, returns False."""
    assert (
        await emit_stream_notification(
            None, settings=_REDIS_SETTINGS, stream=STREAM_DELIVER, fields={"x": "1"}
        )
        is False
    )
