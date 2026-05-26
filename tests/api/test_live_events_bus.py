"""Unit tests for :class:`backend.api.v1.live_events.LiveEventBus` (B16).

The bus is the in-memory fan-out backbone for the SSE endpoint. These tests
exercise the publish / subscribe contract without spinning up FastAPI — they
are the smallest possible Red→Green loop for the producer/consumer wiring.

Workspace isolation is the most important invariant: an event published to
workspace A must NEVER reach a workspace B subscriber. Two parallel
subscribers on the same workspace must each see the event (fan-out, not
hand-off).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.api.v1.live_events import LiveEvent, LiveEventBus

pytestmark = pytest.mark.asyncio


async def test_publish_with_no_subscribers_is_silent() -> None:
    """Publishing into an empty workspace must not raise — events to nobody
    drop on the floor (the audit outbox remains the durable record)."""
    bus = LiveEventBus()
    workspace_id = uuid.uuid4()
    # No subscribers → should complete without error and without buffering.
    await bus.publish(workspace_id, LiveEvent(event_type="decision.pending", data={"foo": "bar"}))


async def test_subscribe_receives_published_event() -> None:
    """One subscriber on workspace W gets a published event for W."""
    bus = LiveEventBus()
    workspace_id = uuid.uuid4()

    received: list[LiveEvent] = []

    async def consume() -> None:
        async with bus.subscribe(workspace_id) as queue:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            received.append(event)

    consumer_task = asyncio.create_task(consume())
    # Let the consumer register itself before we publish.
    await asyncio.sleep(0.05)
    await bus.publish(
        workspace_id, LiveEvent(event_type="decision.pending", data={"decision_id": "abc"})
    )
    await consumer_task

    assert len(received) == 1
    assert received[0].event_type == "decision.pending"
    assert received[0].data == {"decision_id": "abc"}


async def test_workspace_isolation() -> None:
    """A publish on workspace A must NOT reach a workspace B subscriber.

    This is the core multi-tenant safety property — without it the SSE
    endpoint would cross workspace boundaries.
    """
    bus = LiveEventBus()
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()

    seen_in_b: list[LiveEvent] = []

    async def consume_b() -> None:
        async with bus.subscribe(ws_b) as queue:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.3)
                seen_in_b.append(event)
            except TimeoutError:
                return

    consumer_task = asyncio.create_task(consume_b())
    await asyncio.sleep(0.05)
    await bus.publish(ws_a, LiveEvent(event_type="decision.pending", data={"decision_id": "abc"}))
    await consumer_task

    assert seen_in_b == []  # Workspace B saw nothing — perfect isolation.


async def test_multiple_subscribers_same_workspace_all_receive() -> None:
    """Two subscribers on workspace W BOTH get every event — fan-out, not
    hand-off. Two PWA tabs open on the same workspace must both wake up."""
    bus = LiveEventBus()
    workspace_id = uuid.uuid4()

    received_1: list[LiveEvent] = []
    received_2: list[LiveEvent] = []

    async def consume(target: list[LiveEvent]) -> None:
        async with bus.subscribe(workspace_id) as queue:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            target.append(event)

    t1 = asyncio.create_task(consume(received_1))
    t2 = asyncio.create_task(consume(received_2))
    await asyncio.sleep(0.05)
    await bus.publish(workspace_id, LiveEvent(event_type="run.terminal", data={"run_id": "r1"}))
    await asyncio.gather(t1, t2)

    assert len(received_1) == 1
    assert len(received_2) == 1
    assert received_1[0].event_type == "run.terminal"
    assert received_2[0].event_type == "run.terminal"


async def test_unsubscribe_on_context_exit() -> None:
    """After the ``async with`` exits the subscriber is removed — a follow-up
    publish must not raise on a leaked dead queue, and a fresh subscribe must
    not see the prior event."""
    bus = LiveEventBus()
    workspace_id = uuid.uuid4()

    async with bus.subscribe(workspace_id) as queue:
        await bus.publish(
            workspace_id,
            LiveEvent(event_type="decision.pending", data={"decision_id": "abc"}),
        )
        first = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert first.event_type == "decision.pending"

    # Subscriber is gone. Publishing again should drop silently.
    await bus.publish(
        workspace_id, LiveEvent(event_type="decision.pending", data={"decision_id": "def"})
    )

    # A fresh subscriber must not see the prior event (no replay; the bus is
    # not a durable log).
    async with bus.subscribe(workspace_id) as queue2:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue2.get(), timeout=0.1)
