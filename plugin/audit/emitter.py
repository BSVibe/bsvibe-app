"""AuditEmitter — turns a typed event into an outbox row inside the caller's tx."""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from plugin.audit.events import AuditEventBase
from plugin.audit.store import OutboxStore


def _ambient_trace_id() -> str | None:
    bound = structlog.contextvars.get_contextvars()
    value = bound.get("trace_id")
    if isinstance(value, str) and value:
        return value
    return None


class AuditEmitter:
    """Emit one event into the caller's outbox table inside their session."""

    def __init__(self, *, store: OutboxStore | None = None) -> None:
        self._store = store or OutboxStore()
        self._logger = structlog.get_logger("plugin.audit.emitter")

    async def emit(self, event: AuditEventBase, *, session: AsyncSession) -> None:
        if event.trace_id is None:
            ambient = _ambient_trace_id()
            if ambient is not None:
                event.trace_id = ambient

        payload = event.model_dump(mode="json")
        await self._store.insert(
            session,
            event_id=str(event.event_id),
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            payload=payload,
        )
        self._logger.debug(
            "audit_event_enqueued",
            event_type=event.event_type,
            tenant_id=event.tenant_id,
        )
