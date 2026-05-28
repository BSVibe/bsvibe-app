"""Cross-process Redis-transport tests for :class:`LiveEventBus` (C2).

B16 wired SSE live-events through an in-process :class:`LiveEventBus` —
``publish`` on bus A only reaches subscribers on the SAME bus instance, and
``LiveEventBus`` is a process-singleton. In production the audit emit fires in
the worker container (``bsvibe-prod-worker-1``) while the SSE endpoint runs in
the backend HTTP container (``bsvibe-prod-backend-1``); the in-process fan-out
silently drops events across the process boundary and the founder never sees
``run.terminal`` / ``decision.pending`` / ``delivery.queued`` until manual
refresh.

C2 introduces a Redis pub/sub transport keyed per workspace
(``live_events:{workspace_id}``). These tests pin the cross-process
behaviour via TWO :class:`LiveEventBus` instances sharing the same fake
Redis client — emulating "publish in worker process / subscribe in HTTP
process" in a single test.

Invariants exercised:

* publish on bus A → reaches a subscriber on bus B (cross-process via Redis).
* workspace isolation preserved across the Redis hop.
* a redis publish failure does NOT raise into the producer (soft-fail).
* a redis subscribe relay failure does NOT raise into the consumer
  (soft-fail; the in-memory fan-out still works for the local process).
* the in-memory path (no redis injected) still works unchanged — proves the
  redis-aware bus does not regress the default constructor.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import pytest_asyncio

from backend.api.v1.live_events import LiveEvent, LiveEventBus

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Shared fakeredis fixture — flushed between tests for namespace isolation.
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis() -> Any:
    """Return a fresh ``fakeredis.aioredis.FakeRedis`` (decode_responses=True).

    ``decode_responses=True`` matches the production client convention so
    pubsub payloads come back as ``str`` (mirrors :mod:`backend.workers.emit`
    and the executors dispatch substrate).
    """
    try:
        import fakeredis
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:  # pragma: no cover — fakeredis is a declared dev dep
        pytest.skip("fakeredis not installed")
    # Isolated server per instance — the shared default server binds async
    # primitives to one event loop, breaking pytest-asyncio's per-test loops.
    client = fakeredis_aio.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    try:
        await client.flushdb()
        yield client
    finally:
        await client.aclose()


async def _wait_for_publish(bus: LiveEventBus, workspace_id: uuid.UUID, event: LiveEvent) -> None:
    """Publish, then yield enough event-loop ticks for the redis pubsub
    listener in the SUBSCRIBE-side bus to read the message and fan it out
    onto the local queues. ``fakeredis`` is in-process and synchronous from
    the redis side, but the listener is an asyncio Task and needs a turn."""
    await bus.publish(workspace_id, event)
    # A small sleep is unavoidable: fakeredis pub/sub delivery to the
    # listener Task requires the loop to schedule it. Keep this snappy.
    for _ in range(20):
        await asyncio.sleep(0.02)


# --------------------------------------------------------------------------
# In-memory path is preserved when no redis is injected.
# --------------------------------------------------------------------------


async def test_no_redis_falls_back_to_in_memory_fanout() -> None:
    """A bus constructed without redis still fans out in-process — the new
    transport must be purely additive."""
    bus = LiveEventBus()  # no redis
    workspace_id = uuid.uuid4()

    received: list[LiveEvent] = []

    async def consume() -> None:
        async with bus.subscribe(workspace_id) as queue:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            received.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await bus.publish(workspace_id, LiveEvent(event_type="run.terminal", data={"run_id": "r1"}))
    await task

    assert len(received) == 1
    assert received[0].event_type == "run.terminal"
    assert received[0].data == {"run_id": "r1"}


# --------------------------------------------------------------------------
# Cross-process emulation: bus A publishes via Redis → bus B's subscriber
# receives. This is the core C2 delta — was process-local, now cross-process.
# --------------------------------------------------------------------------


async def test_cross_process_publish_relays_to_other_bus_subscriber(
    fake_redis: Any,
) -> None:
    """Two LiveEventBus instances share a Redis backend. A publish on bus A
    must reach a subscriber on bus B — emulates worker-process publish /
    backend-HTTP-process subscribe."""
    workspace_id = uuid.uuid4()
    bus_publisher = LiveEventBus(redis=fake_redis)
    bus_subscriber = LiveEventBus(redis=fake_redis)

    received: list[LiveEvent] = []

    async def consume() -> None:
        async with bus_subscriber.subscribe(workspace_id) as queue:
            event = await asyncio.wait_for(queue.get(), timeout=2.0)
            received.append(event)

    task = asyncio.create_task(consume())
    # Give the relay task time to subscribe to the Redis channel before
    # the publish lands — without this the fakeredis publisher delivers
    # to zero current subscribers.
    await asyncio.sleep(0.1)
    await _wait_for_publish(
        bus_publisher,
        workspace_id,
        LiveEvent(event_type="decision.pending", data={"decision_id": "d1"}),
    )
    await task

    assert len(received) == 1
    assert received[0].event_type == "decision.pending"
    assert received[0].data == {"decision_id": "d1"}


async def test_workspace_isolation_preserved_across_redis_hop(
    fake_redis: Any,
) -> None:
    """A publish on workspace A must NOT reach a workspace B subscriber on
    a different bus — the Redis channel is keyed per workspace, so cross-
    workspace traffic stays partitioned."""
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    bus_publisher = LiveEventBus(redis=fake_redis)
    bus_subscriber = LiveEventBus(redis=fake_redis)

    seen_in_b: list[LiveEvent] = []

    async def consume_b() -> None:
        async with bus_subscriber.subscribe(ws_b) as queue:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.4)
                seen_in_b.append(event)
            except TimeoutError:
                return

    task = asyncio.create_task(consume_b())
    await asyncio.sleep(0.1)
    await _wait_for_publish(
        bus_publisher,
        ws_a,
        LiveEvent(event_type="decision.pending", data={"decision_id": "from-a"}),
    )
    await task

    assert seen_in_b == []  # Workspace B saw nothing — isolation preserved.


# --------------------------------------------------------------------------
# Soft-fail: publish-side / subscribe-side relay failures must not break
# producers or consumers. The in-memory fallback / heartbeat keep working.
# --------------------------------------------------------------------------


class _ExplodingRedis:
    """A redis double whose ``publish`` always raises — used to assert the
    publish path soft-fails. ``pubsub()`` is unreachable in this test path."""

    async def publish(self, channel: str, message: str) -> int:
        raise RuntimeError("redis is on fire")

    def pubsub(self) -> Any:  # pragma: no cover — never invoked in this test
        raise RuntimeError("not used")


async def test_redis_publish_failure_does_not_raise_into_producer() -> None:
    """A Redis publish error inside ``LiveEventBus.publish`` must be swallowed.

    The audit producer's :func:`safe_emit` already has its own catch-all
    swallow, but defence-in-depth: a hung / misconfigured Redis must NOT
    cause the bus to raise into callers that don't have a defensive try
    around ``publish`` (the SSE endpoint may publish heartbeat-ish state in
    the future). Mirrors the soft-fail contract on the existing per-queue
    ``put_nowait`` failure mode.
    """
    bus = LiveEventBus(redis=_ExplodingRedis())
    workspace_id = uuid.uuid4()
    # MUST NOT raise.
    await bus.publish(
        workspace_id, LiveEvent(event_type="decision.pending", data={"decision_id": "x"})
    )


async def test_local_subscriber_still_receives_when_redis_publish_fails(
    fake_redis: Any,
) -> None:
    """When the redis publish raises but the bus has local subscribers, the
    local in-memory fan-out path MUST still deliver. The Redis transport is
    a transport, not a replacement for local delivery — a producer that
    publishes through the SAME bus the SSE handler subscribed against
    should still wake the SSE handler even when Redis is down.
    """
    bus = LiveEventBus(redis=_ExplodingRedis())
    workspace_id = uuid.uuid4()

    received: list[LiveEvent] = []

    async def consume() -> None:
        async with bus.subscribe(workspace_id) as queue:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            received.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await bus.publish(workspace_id, LiveEvent(event_type="run.terminal", data={"run_id": "r1"}))
    await task

    assert len(received) == 1
    assert received[0].event_type == "run.terminal"


# --------------------------------------------------------------------------
# Wire-up: get_live_event_bus accepts a redis client at FIRST call, so
# backend.api.main + backend.workers.run can inject a process-wide client.
# --------------------------------------------------------------------------


async def test_set_live_event_bus_redis_wires_singleton(fake_redis: Any) -> None:
    """Both processes (backend HTTP + worker daemon) need the singleton to
    carry a Redis client. ``set_live_event_bus_redis`` is the wire-up seam —
    called once at app/worker startup.
    """
    from backend.api.v1.live_events import (
        get_live_event_bus,
        reset_live_event_bus_for_testing,
        set_live_event_bus_redis,
    )

    reset_live_event_bus_for_testing()
    try:
        set_live_event_bus_redis(fake_redis)
        bus = get_live_event_bus()
        # The singleton carries the redis client (used to drive cross-
        # process delivery). Probe via the public attribute, kept stable
        # for the wire-up tests.
        assert bus._redis is fake_redis  # noqa: SLF001 — wire-up invariant
    finally:
        reset_live_event_bus_for_testing()
