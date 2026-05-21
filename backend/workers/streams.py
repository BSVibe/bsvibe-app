"""RedisStreamConsumer — consumer-group wrapper over Redis Streams.

Workflow §12.5 #8 (Bundle G — Workers). All worker classes consume
from Redis Streams (XREADGROUP) — *not* Pub/Sub — so we get at-least-once
delivery + per-consumer-group offset tracking.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


StreamHandler = Callable[[dict[str, Any]], Awaitable[None]]


class RedisStreamConsumer:
    """Async XREADGROUP consumer with handler-per-message dispatch.

    The consumer is intentionally provider-agnostic — callers inject
    the redis async client at construction time. The pattern stays
    consumer-group based to give us:

    * at-least-once delivery (XACK only after handler succeeds)
    * worker horizontal scale (multiple processes in one group)
    * lagged-consumer visibility (XPENDING / XLEN)
    """

    async def consume(
        self,
        *,
        stream_name: str,
        consumer_group: str,
        handler: StreamHandler,
    ) -> None:
        """Loop: XREADGROUP → handler → XACK."""
        # TODO(bundle-g-integration): concrete lift from BSNexus
        # backend/workers/_stream_loop.py. Includes XGROUP CREATE
        # MKSTREAM, XREADGROUP with BLOCK ms, XACK on handler success,
        # XPENDING + XCLAIM for stalled-consumer recovery.
        logger.debug(
            "redis_stream_consumer_stub",
            stream_name=stream_name,
            consumer_group=consumer_group,
        )
        raise NotImplementedError("RedisStreamConsumer.consume pending Bundle G integration")


__all__ = ["RedisStreamConsumer", "StreamHandler"]
