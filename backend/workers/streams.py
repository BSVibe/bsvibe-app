"""RedisStreamConsumer — consumer-group wrapper over Redis Streams.

Workflow §12.5 #8 (Bundle G — Workers). The OPT-IN
(``worker_mode="redis_streams"``) trigger for the worker pipeline. All
worker classes consume from Redis Streams (XREADGROUP) — *not* Pub/Sub — so
we get at-least-once delivery + per-consumer-group offset tracking. DB-polling
stays the default; this is purely an additive scale/latency path.

The consumer is intentionally provider-agnostic — callers inject the redis
async client at construction time (``redis.asyncio`` in production, an
in-process fake in tests). The pattern stays consumer-group based to give us:

* at-least-once delivery (XACK only after the handler succeeds — a handler
  failure leaves the entry pending, so it is redelivered);
* worker horizontal scale (multiple processes in one group);
* lagged-consumer visibility (XPENDING / XLEN);
* stalled-consumer recovery (XAUTOCLAIM of long-pending entries).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


StreamHandler = Callable[[dict[str, Any]], Awaitable[None]]


class RedisStreamConsumer:
    """Async XREADGROUP consumer with handler-per-message dispatch.

    ``client`` is any ``redis.asyncio.Redis`` (or compatible fake) configured
    with ``decode_responses=True`` so stream fields decode to ``str``.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _ensure_group(self, stream_name: str, consumer_group: str) -> None:
        """XGROUP CREATE ... MKSTREAM, tolerating an already-existing group.

        ``MKSTREAM`` creates the stream key if absent so the group exists even
        before the first producer XADD; re-creating an existing group raises a
        ``BUSYGROUP`` error which we treat as success (idempotent)."""
        try:
            await self._client.xgroup_create(
                name=stream_name, groupname=consumer_group, id="0", mkstream=True
            )
        except Exception as exc:  # noqa: BLE001 — only BUSYGROUP is benign
            if "BUSYGROUP" not in str(exc):
                raise

    async def consume_once(
        self,
        *,
        stream_name: str,
        consumer_group: str,
        consumer_name: str,
        handler: StreamHandler,
        count: int = 64,
        min_idle_ms: int | None = None,
    ) -> int:
        """Read one batch (new + reclaimed-pending) → handler → XACK each.

        * Ensures the consumer group exists (MKSTREAM).
        * When ``min_idle_ms`` is set, first XAUTOCLAIMs entries pending longer
          than that (stalled-consumer recovery / redelivery); otherwise reads
          only NEW entries (``>``).
        * Runs ``handler(fields)`` per entry; XACKs **only on success**. A
          handler error leaves the entry pending (at-least-once → redelivered
          on a later pass), and is logged, never raised.

        Returns the number of entries the handler processed AND acked.
        """
        await self._ensure_group(stream_name, consumer_group)

        if min_idle_ms is not None:
            entries = await self._autoclaim(
                stream_name, consumer_group, consumer_name, min_idle_ms, count
            )
        else:
            entries = await self._readgroup(stream_name, consumer_group, consumer_name, count)

        acked = 0
        for entry_id, fields in entries:
            try:
                await handler(fields)
            except Exception:  # noqa: BLE001 — leave pending for redelivery
                logger.warning(
                    "redis_stream_handler_failed",
                    stream_name=stream_name,
                    consumer_group=consumer_group,
                    entry_id=entry_id,
                    exc_info=True,
                )
                continue
            await self._client.xack(stream_name, consumer_group, entry_id)
            acked += 1
        return acked

    async def _readgroup(
        self, stream_name: str, consumer_group: str, consumer_name: str, count: int
    ) -> list[tuple[str, dict[str, Any]]]:
        """XREADGROUP new (``>``) entries → flat ``[(id, fields), ...]``."""
        resp = await self._client.xreadgroup(
            groupname=consumer_group,
            consumername=consumer_name,
            streams={stream_name: ">"},
            count=count,
        )
        out: list[tuple[str, dict[str, Any]]] = []
        for _name, entries in resp or []:
            out.extend((entry_id, fields) for entry_id, fields in entries)
        return out

    async def _autoclaim(
        self,
        stream_name: str,
        consumer_group: str,
        consumer_name: str,
        min_idle_ms: int,
        count: int,
    ) -> list[tuple[str, dict[str, Any]]]:
        """XAUTOCLAIM long-pending entries to this consumer → ``[(id, fields)]``.

        XAUTOCLAIM returns ``(cursor, claimed_entries, deleted_ids)``; we take
        the claimed entries (already reassigned to ``consumer_name``) for the
        handler. Entries whose underlying message was trimmed come back with
        ``None`` fields and are skipped."""
        resp = await self._client.xautoclaim(
            name=stream_name,
            groupname=consumer_group,
            consumername=consumer_name,
            min_idle_time=min_idle_ms,
            start_id="0-0",
            count=count,
        )
        # resp shape: [next_cursor, [(id, fields), ...]] (+ deleted ids on >=7).
        claimed = resp[1] if len(resp) > 1 else []
        return [(entry_id, fields) for entry_id, fields in claimed if fields]

    async def consume(
        self,
        *,
        stream_name: str,
        consumer_group: str,
        consumer_name: str,
        handler: StreamHandler,
        stop_event: asyncio.Event,
        idle_sleep_s: float = 0.25,
        reclaim_idle_ms: int = 5000,
    ) -> None:
        """Loop :meth:`consume_once` (new entries) until ``stop_event`` is set.

        The daemon entrypoint (``backend.workers.run`` in Redis mode) runs one
        of these per worker. We deliberately use a *non-blocking* read + a short
        ``idle_sleep_s`` between empty passes rather than a server-side BLOCK
        read: the poll cooperates cleanly with ``stop_event`` / task
        cancellation (a BLOCKing XREADGROUP cannot be interrupted promptly, and
        its semantics differ between real Redis and the in-process fake). The
        latency cost is bounded by ``idle_sleep_s`` (sub-second), still far
        tighter than the workers' 5s DB-poll interval.

        Every pass also reclaims entries pending longer than ``reclaim_idle_ms``
        (a crashed/slow peer's work) so no notification is stranded.
        """
        await self._ensure_group(stream_name, consumer_group)
        while not stop_event.is_set():
            try:
                # Reclaim stalled work, then drain new entries (both via the
                # same XACK-on-success handler path).
                reclaimed = await self.consume_once(
                    stream_name=stream_name,
                    consumer_group=consumer_group,
                    consumer_name=consumer_name,
                    handler=handler,
                    min_idle_ms=reclaim_idle_ms,
                )
                processed = await self.consume_once(
                    stream_name=stream_name,
                    consumer_group=consumer_group,
                    consumer_name=consumer_name,
                    handler=handler,
                )
            except Exception:  # noqa: BLE001 — never let the consume loop die
                logger.exception("redis_stream_consume_iteration_failed", stream_name=stream_name)
                await asyncio.sleep(0.5)
                continue
            if reclaimed == 0 and processed == 0:
                # Idle — back off briefly so we don't busy-spin, but stay
                # responsive to ``stop_event``.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=idle_sleep_s)
                except TimeoutError:
                    continue


__all__ = ["RedisStreamConsumer", "StreamHandler"]
