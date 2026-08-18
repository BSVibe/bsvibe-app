"""Redis-Streams consumer wiring for the worker runtime (§17.2a slice).

Opt-in path: when ``worker_mode="redis_streams"`` the daemon drives each
worker by a Redis Streams consumer (XREADGROUP → handler → XACK) INSTEAD
of the poll loop.  The handler is the worker's OWN single-tick method, so
no business logic is duplicated: Redis is only a different *trigger* for
the same tick.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from backend.workers.base import BaseWorker
from backend.workers.stream_keys import STREAM_KEY_BY_CONSUMER
from backend.workers.streams import RedisStreamConsumer, StreamHandler

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class StreamConsumerBinding:
    """One worker bound to its source stream + consumer group + tick handler."""

    stream_name: str
    consumer_group: str
    handler: StreamHandler


def _tick_handler(tick: Callable[[], Awaitable[int]]) -> StreamHandler:
    """Adapt a worker's no-arg single-tick method to a stream handler.

    The notification fields are intentionally ignored — the worker's tick reads
    its own source table (the DB row is the source of truth); the stream entry
    is only a wake-up."""

    async def _handle(_fields: dict[str, Any]) -> None:
        await tick()

    return _handle


def build_stream_consumers(workers: list[Any]) -> list[StreamConsumerBinding]:
    """Map known workers to their (stream, group, handler) bindings.

    * intake_worker → ``intake`` stream, handler = ``drain_once``
    * agent_worker → ``agent`` stream, handler = ``_tick`` (claim + drive)
    * delivery_worker → ``deliver`` stream, handler = ``drain_once``
    * settle_worker → ``settle`` stream, handler = ``drain_once``

    The relay_worker is intentionally OMITTED — it drains the audit outbox on
    its own cadence, not in response to a producer event, so it has no stream.
    A worker whose name is not in the mapping is skipped (not crashed).

    The worker→stream map is DERIVED from the single
    :data:`~backend.workers.stream_keys.STREAM_KEY_BY_CONSUMER` declaration, so
    this consumer side can no longer drift from the producer-side constants in
    :mod:`backend.workers.emit` (both resolve to the same typed
    :class:`~backend.workers.stream_keys.StreamKey` bindings)."""
    bindings: list[StreamConsumerBinding] = []
    for worker in workers:
        name = getattr(worker, "_name", None)
        if not isinstance(name, str):
            continue
        stream = STREAM_KEY_BY_CONSUMER.get(name)
        if stream is None:
            continue
        # agent_worker advances through claim + drive in one tick (``_tick``);
        # the queue-style workers expose a single ``drain_once``. Both reach
        # the SAME logic — preferring ``_tick`` keeps the trigger faithful to
        # the poll-loop body.
        tick = getattr(worker, "_tick", None)
        if tick is None:
            tick = worker.drain_once
        bindings.append(
            StreamConsumerBinding(
                stream_name=stream,
                consumer_group=name,
                handler=_tick_handler(tick),
            )
        )
    return bindings


async def run_stream_consumers(
    *,
    workers: list[BaseWorker],
    redis_client: Any,
    stop_event: asyncio.Event,
    consumer_name: str = "worker-1",
) -> None:
    """Run a :class:`RedisStreamConsumer` per worker binding until stopped.

    Each consumer loops XREADGROUP → the worker's own tick handler → XACK. The
    relay worker (no stream binding) keeps running on its DB-poll loop so the
    audit outbox still drains; it is started/stopped alongside the consumers."""
    consumer = RedisStreamConsumer(redis_client)
    bindings = build_stream_consumers(list(workers))
    bound_groups = {b.consumer_group for b in bindings}

    # Workers without a stream binding (relay) still poll their own source.
    poll_workers = [w for w in workers if getattr(w, "_name", None) not in bound_groups]
    for w in poll_workers:
        await w.start()

    tasks = [
        asyncio.create_task(
            consumer.consume(
                stream_name=b.stream_name,
                consumer_group=b.consumer_group,
                consumer_name=consumer_name,
                handler=b.handler,
                stop_event=stop_event,
            ),
            name=f"stream::{b.consumer_group}",
        )
        for b in bindings
    ]
    logger.info("worker_runtime_started_redis_streams", streams=sorted(bound_groups))
    try:
        await stop_event.wait()
    finally:
        for w in poll_workers:
            await w.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:  # pragma: no cover — expected on shutdown
                pass


__all__ = [
    "StreamConsumerBinding",
    "build_stream_consumers",
    "run_stream_consumers",
]
