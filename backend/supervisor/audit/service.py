"""Producer-side helpers: ``safe_emit`` + ``make_actor``.

``safe_emit`` swallows emitter failures so the audit infra can never
break a domain write. Lifted from BSupervisor's ``core/audit.py``.

B16 hook: every successful emit ALSO publishes a tiny live-event onto the
in-memory :class:`backend.api.v1.live_events.LiveEventBus` for the
high-signal subset (decision pending, run terminal, delivery queued) so the
PWA SSE subscribers wake up the same moment the durable outbox row lands.
The bridge is soft-imported + soft-fail — an SSE wiring failure must never
break the domain write or the durable audit record.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.supervisor.audit.emitter import AuditEmitter
from backend.supervisor.audit.events import ActorType, AuditActor, AuditEventBase

logger = structlog.get_logger(__name__)

_default_emitter: AuditEmitter = AuditEmitter()


def make_actor(
    *,
    actor_type: ActorType,
    actor_id: str,
    email: str | None = None,
    label: str | None = None,
) -> AuditActor:
    return AuditActor(type=actor_type, id=actor_id, email=email, label=label)


async def _bridge_to_live_event_bus(event: AuditEventBase) -> None:
    """Forward high-signal audit events onto the SSE LiveEventBus (B16).

    Soft-import + soft-fail. The audit producer doesn't depend on the SSE
    infrastructure being importable / running — a failure here logs but
    never propagates back to the domain write.
    """
    try:
        from backend.api.v1.live_events import (  # noqa: PLC0415
            LiveEvent,
            get_live_event_bus,
            map_audit_event_type,
        )

        sse_event_type = map_audit_event_type(event.event_type)
        if sse_event_type is None:
            return  # Not one of the three SSE-surfaced types.
        if not event.workspace_id:
            return  # Workspace isolation requires a workspace_id.
        try:
            workspace_uuid = uuid.UUID(event.workspace_id)
        except (ValueError, AttributeError):
            return
        bus = get_live_event_bus()
        # Forward a small subset of fields — the consumer just needs enough
        # to know "wake up + refetch", not the whole payload.
        data: dict[str, object] = {
            "event_id": str(event.event_id),
            "occurred_at": event.occurred_at.isoformat(),
        }
        if event.resource is not None:
            data["resource_type"] = event.resource.type
            data["resource_id"] = event.resource.id
        # Forward a few well-known small keys the PWA may want to highlight
        # one specific item.
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


async def safe_emit(
    event: AuditEventBase,
    *,
    session: AsyncSession,
    emitter: AuditEmitter | None = None,
) -> None:
    """Emit an audit event without ever raising into the request handler.

    On a successful emit, ALSO publishes a tiny live-event onto the
    in-memory bus for the high-signal subset (B16) so the PWA SSE
    subscribers wake up immediately. The bridge is soft-fail — a publish
    error never propagates back to the caller (the durable outbox row
    remains the source of truth).
    """
    try:
        await (emitter or _default_emitter).emit(event, session=session)
    except Exception:  # noqa: BLE001 — audit must never break the domain write
        logger.warning(
            "supervisor_audit_emit_failed",
            event_type=getattr(event, "event_type", None),
            exc_info=True,
        )
        return
    # Defense-in-depth: ``_bridge_to_live_event_bus`` is soft-fail internally,
    # but a stale singleton bus bound to a closed test event loop has surfaced
    # loop-binding RuntimeErrors that escape via the await chain (the redis
    # client's disconnect path re-raises during exception cleanup). Wrap the
    # bridge call here so a bus-side hiccup can never propagate into the
    # producer — the executor orchestrator wraps a verify try/except around
    # this, and a leaking error mapped a Decision path to system_error.
    try:
        await _bridge_to_live_event_bus(event)
    except BaseException:  # noqa: BLE001 — last-resort guard, never propagate
        logger.warning(
            "supervisor_audit_bridge_outer_guard",
            event_type=getattr(event, "event_type", None),
            exc_info=True,
        )
