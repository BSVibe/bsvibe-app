"""Live event bus + SSE wire helpers (B16 + C2).

Per Workflow §4 ("SSE for live UX state — no polling") the PWA should learn
about high-signal events as they happen (decision pending, run terminal,
Safe-Mode delivery queued) without GET-polling. Before B16 the backend had
zero SSE endpoints, so the founder discovered paused runs / new deliveries
only by manually refreshing.

This module provides:

* :class:`LiveEvent` — the tiny wire shape (event_type + data dict).
* :class:`LiveEventBus` — a per-workspace fan-out registry. In-process
  fan-out is the default; an optional Redis pub/sub transport (C2) lets a
  publish on one process reach subscribers on another. ``publish`` drops on
  the floor when no one is subscribed (the audit DB outbox remains the
  durable record); ``subscribe`` is a context manager that yields an
  ``asyncio.Queue`` and unwinds the registration on exit.
* :func:`get_live_event_bus` — process-wide singleton accessor used by the
  audit producer + the SSE handler.
* :func:`set_live_event_bus_redis` — wire-up seam called once at process
  startup (FastAPI ``create_app`` + ``run_workers``) so both processes'
  singletons share the same Redis transport.
* :func:`encode_sse` / :func:`encode_heartbeat` — the wire helpers the SSE
  endpoint uses to frame bytes.

C2 design (the cross-process fix): in prod the audit emit fires inside the
worker container while the SSE endpoint runs in the backend HTTP container,
and ``LiveEventBus`` is a per-process singleton — so the publish on bus A
silently dropped on the floor as far as the SSE bus B was concerned. The
fix introduces an optional Redis pub/sub transport keyed per workspace
(``live_events:{workspace_id}``). When a Redis client is bound to the bus,
``publish`` BOTH fans out locally AND publishes on the workspace channel,
and ``subscribe`` ensures a per-workspace listener task is running that
relays Redis-delivered messages onto every local queue for that workspace.
The listener is reference-counted (started on first subscriber, stopped on
last). The transport is purely additive — no Redis injected → behaviour is
identical to B16's in-memory fan-out (kept for tests, dev, fallback).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# The high-signal event types this surface carries. Other event types (LLM
# turns, tool calls, etc.) remain in the audit outbox only — adding more SSE
# event types here is a future, focused lift.
EVENT_DECISION_PENDING = "decision.pending"
EVENT_RUN_TERMINAL = "run.terminal"
EVENT_DELIVERY_QUEUED = "delivery.queued"
# D6 — a mid-loop partial Deliverable just landed (Synthesis §13 / Workflow §1).
# Published DIRECTLY by the agent loop's ``emit_deliverable`` handler (not from
# the audit bridge) so the Run / Brief views can render the partial AS IT IS
# EMITTED instead of only on the verified terminal.
EVENT_DELIVERABLE_PARTIAL = "deliverable.partial"

# Audit ``event_type`` → SSE ``event_type`` for the producer bridge. The audit
# stream uses dotted-prefix names (``execution.decision.pending``); the SSE
# wire uses short names so consumers stay independent of the backend's
# internal taxonomy.
_AUDIT_TO_SSE_EVENT_TYPE: dict[str, str] = {
    "execution.decision.pending": EVENT_DECISION_PENDING,
    "execution.loop.terminal": EVENT_RUN_TERMINAL,
    "delivery.queued": EVENT_DELIVERY_QUEUED,
}


# Redis channel prefix — per-workspace channel name is
# ``live_events:<workspace_uuid>``. Keeping the prefix short keeps PUBSUB
# CHANNELS output legible in ops debug + matches the
# ``backend.workers.emit`` / ``backend.executors.dispatch`` short-prefix
# style for stream / channel identifiers.
_REDIS_CHANNEL_PREFIX = "live_events:"


def _channel_for(workspace_id: uuid.UUID) -> str:
    """Return the Redis pub/sub channel name for ``workspace_id``."""
    return f"{_REDIS_CHANNEL_PREFIX}{workspace_id}"


# ``_RedisPubSub`` is intentionally typed ``Any`` rather than a Protocol —
# the real ``redis.asyncio.Redis`` (and ``fakeredis.aioredis.FakeRedis``)
# have wide overloaded signatures (``publish(channel: bytes | str | ...,
# message: int | float | bytes | str | ...) -> Awaitable[Any] | Any``)
# that don't structurally satisfy a narrow Protocol without a cast at
# every wire-up site. The existing :mod:`backend.workers.run` /
# :mod:`backend.executors.dispatch` modules use the same ``Any`` convention
# for the redis client kwarg — matching that choice keeps the wire-up
# call sites cast-free.
_RedisPubSub = Any


@dataclass(frozen=True)
class LiveEvent:
    """One event delivered over the SSE wire.

    ``data`` is whatever JSON-serialisable payload the producer wants to hand
    the consumer — kept TINY (ids only, no LLM content) so the bus stays
    cheap and the consumer just uses it as a wake-up signal to re-fetch.
    """

    event_type: str
    data: dict[str, Any] = field(default_factory=dict)


def _encode_for_redis(event: LiveEvent) -> str:
    """Encode a :class:`LiveEvent` as a JSON string for the Redis channel.

    A flat ``{"event_type": ..., "data": ...}`` envelope so the subscribe
    side can rebuild the dataclass without ambiguity. Separators are
    compact — the bus carries thousands of small events, not big payloads.
    """
    return json.dumps(
        {"event_type": event.event_type, "data": event.data},
        separators=(",", ":"),
    )


def _decode_from_redis(payload: str) -> LiveEvent | None:
    """Inverse of :func:`_encode_for_redis`. ``None`` when the payload is
    malformed — a corrupt entry must NOT break the subscriber loop."""
    try:
        decoded = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    event_type = decoded.get("event_type")
    data = decoded.get("data")
    if not isinstance(event_type, str):
        return None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None
    return LiveEvent(event_type=event_type, data=data)


class LiveEventBus:
    """Per-workspace fan-out with optional Redis pub/sub transport.

    In-memory fan-out is the always-on path: subscribers register an
    :class:`asyncio.Queue` for their workspace via :meth:`subscribe`, and
    :meth:`publish` enqueues the event onto every queue registered for
    that workspace. When a Redis client is bound (``redis`` constructor
    kwarg, or :func:`set_live_event_bus_redis` on the singleton),
    :meth:`publish` ALSO publishes the event onto a per-workspace channel,
    and :meth:`subscribe` ensures a per-workspace listener task is running
    that relays inbound Redis messages onto every local queue for that
    workspace. Reference-counted — the listener starts on the first
    subscriber and stops on the last.

    Cross-workspace isolation is structural — the local registry AND the
    Redis channel are keyed by ``workspace_id``, so a publish on workspace
    A cannot reach a workspace B subscriber, in-process or cross-process.

    All Redis I/O is **soft-fail**: publish errors log + degrade (the local
    fan-out still happens) and listener errors log + the listener restarts
    on the next subscribe. The audit outbox row remains the durable
    record — the bus is only the wake-up signal.
    """

    def __init__(self, *, redis: _RedisPubSub | None = None) -> None:
        # workspace_id → set of queues (each queue belongs to one subscriber).
        # No asyncio.Lock here: set add/discard are atomic under CPython's GIL
        # for our usage (single bucket per workspace, short critical sections),
        # and an asyncio.Lock would bind to the FIRST event loop that touches
        # the singleton — every later test loop's publish would raise
        # "Lock is bound to a different event loop", a failure mode the
        # soft-fail bridge above might not catch in every Python version.
        # Singleton is per-process; per-call snapshots are taken without a lock.
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue[LiveEvent]]] = {}
        # C2 — optional Redis transport. When set, :meth:`publish` ALSO
        # publishes on the workspace channel and :meth:`subscribe` runs a
        # listener task that relays inbound messages onto the local queues.
        self._redis: _RedisPubSub | None = redis
        # Per-workspace background listener tasks (None means "no listener
        # for this workspace yet"). Reference-counted by len(subscribers).
        self._relay_tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # Wire-up — bind a Redis client to the singleton at app/worker startup.
    # ------------------------------------------------------------------
    def bind_redis(self, redis: _RedisPubSub | None) -> None:
        """Bind (or unbind, with ``None``) a Redis transport to this bus.

        Called once per process at startup by :func:`set_live_event_bus_redis`.
        Re-binding while listeners are running is supported but uncommon —
        existing relay tasks keep running against the previously-bound
        client until they exit; the next subscribe rebuilds the listener
        against the new client.
        """
        self._redis = redis

    # ------------------------------------------------------------------
    # Subscribe path — in-memory queue, plus a per-workspace Redis relay
    # when a transport is bound.
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def subscribe(self, workspace_id: uuid.UUID) -> AsyncIterator[asyncio.Queue[LiveEvent]]:
        """Register a subscriber queue for ``workspace_id``.

        Yields an :class:`asyncio.Queue`; on context exit the queue is
        deregistered, so producers no longer fan out into it. When a Redis
        transport is bound and this is the first subscriber for the
        workspace, a background listener task is started that relays
        inbound Redis messages onto every local queue for the workspace.
        When this is the last subscriber to exit, the listener task is
        cancelled (refcount → 0).
        """
        queue: asyncio.Queue[LiveEvent] = asyncio.Queue()
        bucket = self._subscribers.setdefault(workspace_id, set())
        bucket.add(queue)
        # Start the per-workspace Redis listener on first subscriber.
        await self._ensure_redis_listener(workspace_id)
        try:
            yield queue
        finally:
            bucket2 = self._subscribers.get(workspace_id)
            if bucket2 is not None:
                bucket2.discard(queue)
                if not bucket2:
                    # del is safe even under concurrent subscribe — if a
                    # racing subscriber re-populated the set we just lose its
                    # entry from the registry; it sets a fresh queue anyway.
                    self._subscribers.pop(workspace_id, None)
                    # Stop the relay listener — no local subscribers left.
                    await self._stop_redis_listener(workspace_id)

    # ------------------------------------------------------------------
    # Publish path — local fan-out + optional Redis hop.
    # ------------------------------------------------------------------
    async def publish(self, workspace_id: uuid.UUID, event: LiveEvent) -> None:
        """Fan an event out to every subscriber on ``workspace_id``.

        Local in-memory fan-out always runs (drops silently when no one
        is listening on this process). When a Redis transport is bound,
        the event is ALSO published on the workspace channel so subscribers
        on other processes receive it via their relay listener. Both legs
        are soft-fail: a Redis publish error logs + degrades (the local
        fan-out still happens), a per-queue ``put`` failure logs + the
        other queues still get the event.
        """
        # ── local in-memory fan-out (always; same as B16) ───────────────
        bucket = self._subscribers.get(workspace_id)
        # Snapshot the queues — no lock needed; set membership reads are
        # atomic and we tolerate racing subscribe/unsubscribe (a brand-new
        # subscriber may miss this single publish but will catch the next).
        queues = list(bucket) if bucket else []
        for queue in queues:
            try:
                queue.put_nowait(event)
            except Exception:  # noqa: BLE001 — never let one slow consumer break others
                logger.warning(
                    "live_event_publish_failed",
                    workspace_id=str(workspace_id),
                    event_type=event.event_type,
                    exc_info=True,
                )

        # ── Redis pub/sub leg (C2) — soft-fail ─────────────────────────
        redis = self._redis
        if redis is None:
            return
        try:
            await redis.publish(_channel_for(workspace_id), _encode_for_redis(event))
        except Exception:  # noqa: BLE001 — Redis must never break the producer
            logger.warning(
                "live_event_redis_publish_failed",
                workspace_id=str(workspace_id),
                event_type=event.event_type,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Redis listener — per-workspace XREAD-style background task that
    # relays inbound messages onto every local queue for that workspace.
    # ------------------------------------------------------------------
    async def _ensure_redis_listener(self, workspace_id: uuid.UUID) -> None:
        """Start the per-workspace Redis listener if a transport is bound
        and no listener is already running for the workspace."""
        if self._redis is None:
            return
        if workspace_id in self._relay_tasks:
            existing = self._relay_tasks[workspace_id]
            if not existing.done():
                return  # already running
            # A previous listener exited (transport error / cancel) — drop
            # the reference and start a fresh one below.
            self._relay_tasks.pop(workspace_id, None)
        # Setup synchronously enough that the FIRST publish racing the
        # subscribe still has a chance to be delivered — we subscribe to
        # the channel BEFORE the asyncio.create_task returns to the caller.
        ready = asyncio.Event()
        task = asyncio.create_task(
            self._redis_listener(workspace_id, ready),
            name=f"live_events_relay::{workspace_id}",
        )
        self._relay_tasks[workspace_id] = task
        # Wait until the listener has actually subscribed to the channel
        # — without this a publish that races the subscribe lands on zero
        # current Redis subscribers and is lost. A short bounded wait so a
        # misconfigured Redis (listener never reaches ready) can't hang
        # subscribe() indefinitely; the listener still runs once subscribed.
        try:
            await asyncio.wait_for(ready.wait(), timeout=2.0)
        except TimeoutError:
            logger.warning(
                "live_event_redis_listener_ready_timeout",
                workspace_id=str(workspace_id),
            )

    async def _stop_redis_listener(self, workspace_id: uuid.UUID) -> None:
        """Cancel the per-workspace listener task (refcount → 0)."""
        task = self._relay_tasks.pop(workspace_id, None)
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # expected on cancellation
        except Exception:  # noqa: BLE001 — teardown best-effort
            logger.warning(
                "live_event_redis_listener_teardown_failed",
                workspace_id=str(workspace_id),
                exc_info=True,
            )

    def _fan_inbound_to_local_queues(self, workspace_id: uuid.UUID, event: LiveEvent) -> None:
        """Deliver a Redis-inbound event to every local subscriber queue.

        Soft-fail per queue — a stuck consumer must not poison the relay
        loop. Deliberately does NOT republish onto Redis (loop guard).
        """
        bucket = self._subscribers.get(workspace_id)
        queues = list(bucket) if bucket else []
        for queue in queues:
            try:
                queue.put_nowait(event)
            except Exception:  # noqa: BLE001 — never poison the listener
                logger.warning(
                    "live_event_relay_put_failed",
                    workspace_id=str(workspace_id),
                    event_type=event.event_type,
                    exc_info=True,
                )

    async def _handle_pubsub_message(
        self, workspace_id: uuid.UUID, message: dict[str, Any]
    ) -> None:
        """Decode + relay one Redis pub/sub message. No-op on unknown shape."""
        if message.get("type") != "message":
            return
        payload = message.get("data")
        if not isinstance(payload, str):
            return
        event = _decode_from_redis(payload)
        if event is None:
            logger.warning(
                "live_event_redis_decode_failed",
                workspace_id=str(workspace_id),
            )
            return
        self._fan_inbound_to_local_queues(workspace_id, event)

    async def _redis_listener(self, workspace_id: uuid.UUID, ready: asyncio.Event) -> None:
        """Subscribe to the workspace channel + relay inbound messages.

        Runs until cancelled (last subscriber exited). All errors are
        logged + swallowed — Redis hiccups must NOT propagate into the
        SSE consumers, and a malformed message MUST NOT break the loop.
        Sets ``ready`` once the channel is actually subscribed so the
        caller knows producers may publish without race-losing the first
        message.
        """
        redis = self._redis
        if redis is None:  # pragma: no cover — guarded by _ensure_redis_listener
            ready.set()
            return
        channel = _channel_for(workspace_id)
        pubsub = None
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(channel)
            ready.set()
            while True:
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — Redis transport hiccup
                    logger.warning(
                        "live_event_redis_get_message_failed",
                        workspace_id=str(workspace_id),
                        exc_info=True,
                    )
                    # Back off briefly so a tight error loop can't dominate
                    # the event loop; the next get_message should resync.
                    await asyncio.sleep(0.1)
                    continue
                if message is None:
                    # Timeout window with no message — keep spinning so
                    # cancellation is honoured promptly.
                    continue
                await self._handle_pubsub_message(workspace_id, message)
        except asyncio.CancelledError:
            # Normal shutdown — last subscriber exited.
            raise
        except Exception:  # noqa: BLE001 — never raise into the runtime
            logger.warning(
                "live_event_redis_listener_failed",
                workspace_id=str(workspace_id),
                exc_info=True,
            )
            # Unblock anyone awaiting ready so subscribe() doesn't hang.
            if not ready.is_set():
                ready.set()
        finally:
            await _teardown_pubsub(pubsub, channel, workspace_id)


async def _teardown_pubsub(pubsub: Any, channel: str, workspace_id: uuid.UUID) -> None:
    """Best-effort unsubscribe + aclose. Logs (and swallows) teardown errors."""
    if pubsub is None:
        return
    try:
        await pubsub.unsubscribe(channel)
    except Exception:  # noqa: BLE001 — teardown best-effort
        logger.debug(
            "live_event_redis_unsubscribe_failed",
            workspace_id=str(workspace_id),
            exc_info=True,
        )
    try:
        await pubsub.aclose()
    except Exception:  # noqa: BLE001 — teardown best-effort
        logger.debug(
            "live_event_redis_aclose_failed",
            workspace_id=str(workspace_id),
            exc_info=True,
        )


# Process-wide singleton. Test code can construct an isolated
# :class:`LiveEventBus` directly; the SSE endpoint + audit producer always
# reach for this shared instance.
_BUS: LiveEventBus | None = None


def get_live_event_bus() -> LiveEventBus:
    """Return the process-wide :class:`LiveEventBus` singleton."""
    global _BUS  # noqa: PLW0603
    if _BUS is None:
        _BUS = LiveEventBus()
    return _BUS


def set_live_event_bus_redis(redis: _RedisPubSub | None) -> None:
    """Bind (or unbind) a Redis transport on the singleton bus.

    Called once per process at startup:

    * :func:`backend.api.main.create_app` → wires the HTTP container's
      singleton against the configured Redis URL so SSE subscribers
      receive cross-process publishes.
    * :func:`backend.workers.run.run_workers` → wires the worker container's
      singleton against the SAME Redis URL so audit-emit publishes land
      on the channel the HTTP container is subscribed to.

    Passing ``None`` (or skipping the call entirely) keeps the in-memory
    fallback — useful for unit tests, dev without Redis, and the test
    fixtures that construct an isolated bus.
    """
    bus = get_live_event_bus()
    bus.bind_redis(redis)


def reset_live_event_bus_for_testing() -> None:
    """Drop the singleton so each test starts with a fresh registry.

    Used by SSE endpoint tests that need workspace isolation across test
    cases.
    """
    global _BUS  # noqa: PLW0603
    _BUS = None


def map_audit_event_type(audit_event_type: str) -> str | None:
    """Map an audit event ``event_type`` to its short SSE name, or None when
    the audit event isn't one of the high-signal types this lift surfaces.
    """
    return _AUDIT_TO_SSE_EVENT_TYPE.get(audit_event_type)


def encode_sse(event: LiveEvent) -> bytes:
    """Encode a :class:`LiveEvent` as a single ``text/event-stream`` frame.

    Wire format (per WHATWG / RFC):

    .. code-block:: text

        event: decision.pending
        data: {"decision_id": "..."}
        \\n

    A blank line terminates the event. UTF-8 bytes — StreamingResponse
    iterates ``bytes`` so we do the encode here once.
    """
    payload = json.dumps(event.data, separators=(",", ":"))
    frame = f"event: {event.event_type}\ndata: {payload}\n\n"
    return frame.encode("utf-8")


def encode_heartbeat() -> bytes:
    """Encode an SSE comment line that proxies treat as keepalive.

    A line starting with ``:`` is an SSE comment — the browser EventSource
    ignores it, but it keeps idle TCP connections open through proxies that
    would otherwise close them after ~30s of silence.
    """
    return b": ping\n\n"


__all__ = [
    "EVENT_DECISION_PENDING",
    "EVENT_DELIVERABLE_PARTIAL",
    "EVENT_DELIVERY_QUEUED",
    "EVENT_RUN_TERMINAL",
    "LiveEvent",
    "LiveEventBus",
    "encode_heartbeat",
    "encode_sse",
    "get_live_event_bus",
    "map_audit_event_type",
    "reset_live_event_bus_for_testing",
    "set_live_event_bus_redis",
]
