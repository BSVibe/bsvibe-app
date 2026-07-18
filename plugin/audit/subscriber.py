"""``AuditEventSubscriber`` — the EventBus subscriber the audit plugin
registers (Lift R2a).

Receives every event whose ``kind`` starts with ``audit.`` and persists the
carried :class:`plugin.audit.events.AuditEventBase` into ``audit_outbox``
through the producer's :class:`AsyncSession`. Because the bus is synchronous
the insert lands inside the producer's open transaction — transactional
outbox semantics are preserved across the rewire.

The subscriber ALSO bridges high-signal events onto the in-process SSE
LiveEventBus (B16), preserving the soft-fail bridge from the pre-R2a
``safe_emit`` path.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from bsvibe_sdk import Event
from plugin.audit.channels import AUDIT_EMIT_KIND, AUDIT_KIND_PREFIX
from plugin.audit.emitter import AuditEmitter
from plugin.audit.events import AuditEventBase

logger = structlog.get_logger(__name__)

# ``AUDIT_EMIT_KIND`` / ``AUDIT_KIND_PREFIX`` are declared canonically on the
# ``AUDIT_EMIT`` channel (:mod:`plugin.audit.channels`) and re-exported here
# for the runtime prefix guard below + existing importers.


class AuditEventSubscriber:
    """Persists audit events to the outbox inside the producer's session."""

    def __init__(self, *, emitter: AuditEmitter | None = None) -> None:
        self._emitter = emitter or AuditEmitter()

    async def on_event(self, event: Event) -> None:
        if not event.kind.startswith(AUDIT_KIND_PREFIX):
            return
        audit_event = event.payload.get("event")
        session = event.payload.get("session")
        if not isinstance(audit_event, AuditEventBase):
            logger.warning(
                "audit_subscriber_dropped_event",
                reason="payload.event missing or wrong type",
                kind=event.kind,
            )
            return
        if not isinstance(session, AsyncSession):
            logger.warning(
                "audit_subscriber_dropped_event",
                reason="payload.session missing or wrong type",
                kind=event.kind,
            )
            return
        await self._emitter.emit(audit_event, session=session)
        await _bridge_to_live_event_bus(audit_event)


async def _bridge_to_live_event_bus(event: AuditEventBase) -> None:
    """Forward high-signal audit events onto the SSE LiveEventBus (B16).

    Soft-import + soft-fail. Mirrors the pre-R2a bridge in
    ``plugin/audit/service.py`` — keeps PWA SSE subscribers waking up on
    the same emit. Any failure here is swallowed (the durable outbox row
    is the source of truth).
    """
    try:
        from backend.api.v1.live_events import (  # noqa: PLC0415
            LiveEvent,
            get_live_event_bus,
            map_audit_event_type,
        )

        sse_event_type = map_audit_event_type(event.event_type)
        if sse_event_type is None:
            return
        if not event.workspace_id:
            return
        try:
            workspace_uuid = uuid.UUID(event.workspace_id)
        except (ValueError, AttributeError):
            return
        bus = get_live_event_bus()
        data: dict[str, object] = {
            "event_id": str(event.event_id),
            "occurred_at": event.occurred_at.isoformat(),
        }
        if event.resource is not None:
            data["resource_type"] = event.resource.type
            data["resource_id"] = event.resource.id
        for key in ("run_id", "decision_id", "delivery_id", "checkpoint_id"):
            value = event.data.get(key) if isinstance(event.data, dict) else None
            if isinstance(value, (str, int)):
                data[key] = value
        await bus.publish(workspace_uuid, LiveEvent(event_type=sse_event_type, data=data))
    except Exception:  # noqa: BLE001 — never propagate back into the producer
        logger.warning(
            "live_event_bridge_failed",
            event_type=getattr(event, "event_type", None),
            exc_info=True,
        )


__all__ = [
    "AUDIT_EMIT_KIND",
    "AUDIT_KIND_PREFIX",
    "AuditEventSubscriber",
]
