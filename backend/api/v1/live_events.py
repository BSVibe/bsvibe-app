"""In-memory live event bus + SSE endpoint (B16).

Per Workflow §4 ("SSE for live UX state — no polling") the PWA should learn
about high-signal events as they happen (decision pending, run terminal,
Safe-Mode delivery queued) without GET-polling. Before B16 the backend had
zero SSE endpoints, so the founder discovered paused runs / new deliveries
only by manually refreshing.

This module provides:

* :class:`LiveEvent` — the tiny wire shape (event_type + data dict).
* :class:`LiveEventBus` — a per-workspace asyncio fan-out registry. ``publish``
  drops on the floor when no one is subscribed (the audit DB outbox remains
  the durable record); ``subscribe`` is a context manager that yields an
  ``asyncio.Queue`` and unwinds the registration on exit.
* :func:`get_live_event_bus` — process-wide singleton accessor used by the
  audit producer + the SSE handler.
* :func:`live_event_sse_stream` — the StreamingResponse body generator,
  including the periodic heartbeat (``: ping``) so idle connections stay open
  through proxies / middleware.

The SSE endpoint itself lives in :mod:`backend.api.v1.events` (separate
module so this one stays domain-pure, no FastAPI dependency).

Design choice: in-process asyncio fan-out, not Redis pub/sub. The founder
deployment is a single backend process; the audit DB outbox already
guarantees durability of the rich event stream, so this bus only carries the
"wake up" signal. If/when we scale to multiple backend replicas the
:class:`LiveEventBus` interface stays the same — the implementation swaps to
Redis pub/sub behind it.
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


# The three high-signal event types this lift carries. Other event types
# (LLM turns, tool calls, etc.) remain in the audit outbox only — adding more
# SSE event types here is a future, focused lift.
EVENT_DECISION_PENDING = "decision.pending"
EVENT_RUN_TERMINAL = "run.terminal"
EVENT_DELIVERY_QUEUED = "delivery.queued"

# Audit ``event_type`` → SSE ``event_type`` for the producer bridge. The audit
# stream uses dotted-prefix names (``execution.decision.pending``); the SSE
# wire uses short names so consumers stay independent of the backend's
# internal taxonomy.
_AUDIT_TO_SSE_EVENT_TYPE: dict[str, str] = {
    "execution.decision.pending": EVENT_DECISION_PENDING,
    "execution.loop.terminal": EVENT_RUN_TERMINAL,
    "delivery.queued": EVENT_DELIVERY_QUEUED,
}


@dataclass(frozen=True)
class LiveEvent:
    """One event delivered over the SSE wire.

    ``data`` is whatever JSON-serialisable payload the producer wants to hand
    the consumer — kept TINY (ids only, no LLM content) so the bus stays
    cheap and the consumer just uses it as a wake-up signal to re-fetch.
    """

    event_type: str
    data: dict[str, Any] = field(default_factory=dict)


class LiveEventBus:
    """Per-workspace in-memory fan-out.

    Subscribers register an :class:`asyncio.Queue` for their workspace via
    :meth:`subscribe`. :meth:`publish` enqueues the event onto EVERY queue
    registered for that workspace (fan-out) and silently drops events when
    no one is listening (the audit DB row remains the durable record).

    Cross-workspace isolation is structural — the registry is keyed by
    ``workspace_id``, so a publish on workspace A cannot reach a workspace B
    subscriber.
    """

    def __init__(self) -> None:
        # workspace_id → set of queues (each queue belongs to one subscriber).
        # No asyncio.Lock here: set add/discard are atomic under CPython's GIL
        # for our usage (single bucket per workspace, short critical sections),
        # and an asyncio.Lock would bind to the FIRST event loop that touches
        # the singleton — every later test loop's publish would raise
        # "Lock is bound to a different event loop", a failure mode the
        # soft-fail bridge above might not catch in every Python version.
        # Singleton is per-process; per-call snapshots are taken without a lock.
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue[LiveEvent]]] = {}

    @asynccontextmanager
    async def subscribe(self, workspace_id: uuid.UUID) -> AsyncIterator[asyncio.Queue[LiveEvent]]:
        """Register a subscriber queue for ``workspace_id``.

        Yields an :class:`asyncio.Queue`; on context exit the queue is
        deregistered, so producers no longer fan out into it.
        """
        queue: asyncio.Queue[LiveEvent] = asyncio.Queue()
        self._subscribers.setdefault(workspace_id, set()).add(queue)
        try:
            yield queue
        finally:
            bucket = self._subscribers.get(workspace_id)
            if bucket is not None:
                bucket.discard(queue)
                if not bucket:
                    # del is safe even under concurrent subscribe — if a
                    # racing subscriber re-populated the set we just lose its
                    # entry from the registry; it sets a fresh queue anyway.
                    self._subscribers.pop(workspace_id, None)

    async def publish(self, workspace_id: uuid.UUID, event: LiveEvent) -> None:
        """Fan an event out to every subscriber on ``workspace_id``.

        Drops silently when no one is listening (the audit DB row is the
        durable record). Per-queue ``put`` failures are logged + swallowed so
        one stuck consumer cannot poison the rest.
        """
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
    "EVENT_DELIVERY_QUEUED",
    "EVENT_RUN_TERMINAL",
    "LiveEvent",
    "LiveEventBus",
    "encode_heartbeat",
    "encode_sse",
    "get_live_event_bus",
    "map_audit_event_type",
    "reset_live_event_bus_for_testing",
]
