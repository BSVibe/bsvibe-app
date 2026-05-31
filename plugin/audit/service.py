"""Producer-side helpers: ``safe_emit`` + ``make_actor``.

Lift R2a (v8 §13 + D5 audit-as-plugin) re-routes ``safe_emit`` through the
in-process :class:`backend.extensions.eventbus.InProcessEventBus`: the
producer publishes an :class:`bsvibe_sdk.Event` of kind ``audit.emit`` and
the audit plugin's :class:`AuditEventSubscriber` persists it to
``audit_outbox`` synchronously inside the producer's session. The B16 SSE
bridge moves into the subscriber so the producer no longer holds direct
references to the SSE bus.

``safe_emit`` still swallows all failures — the EventBus impl already
catches subscriber exceptions, and the outer try wraps the publish call
itself so a bus-side hiccup (e.g. a stale singleton) can never break the
domain write.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.extensions.eventbus import get_event_bus
from bsvibe_sdk import Event
from plugin.audit.emitter import AuditEmitter
from plugin.audit.events import ActorType, AuditActor, AuditEventBase
from plugin.audit.subscriber import AUDIT_EMIT_KIND

logger = structlog.get_logger(__name__)


def make_actor(
    *,
    actor_type: ActorType,
    actor_id: str,
    email: str | None = None,
    label: str | None = None,
) -> AuditActor:
    return AuditActor(type=actor_type, id=actor_id, email=email, label=label)


async def safe_emit(
    event: AuditEventBase,
    *,
    session: AsyncSession,
    emitter: AuditEmitter | None = None,
) -> None:
    """Emit an audit event without ever raising into the request handler.

    Publishes ``Event(kind="audit.emit", payload={"event": ..., "session": ...})``
    onto the in-process EventBus. The audit plugin's
    :class:`AuditEventSubscriber` (registered on plugin import) persists the
    event to ``audit_outbox`` synchronously inside the producer's
    transaction and forwards the high-signal subset onto the SSE
    LiveEventBus (B16).

    ``emitter`` is honored for test isolation — when supplied, a one-shot
    direct emit through that emitter is used instead of the bus singleton.
    Production code never passes ``emitter``.
    """
    if emitter is not None:
        # Test-isolation path: emit directly through the supplied emitter so
        # tests that inject a failing/spying emitter don't have to register
        # a subscriber on the global bus. The SSE bridge still fires on
        # success — it's a soft-fail observer either way (parity with the
        # pre-R2a behaviour).
        from plugin.audit.subscriber import _bridge_to_live_event_bus  # noqa: PLC0415

        try:
            await emitter.emit(event, session=session)
        except Exception:  # noqa: BLE001
            logger.warning(
                "supervisor_audit_emit_failed",
                event_type=getattr(event, "event_type", None),
                exc_info=True,
            )
            return
        try:
            await _bridge_to_live_event_bus(event)
        except BaseException:  # noqa: BLE001 — last-resort guard
            logger.warning(
                "supervisor_audit_bridge_outer_guard",
                event_type=getattr(event, "event_type", None),
                exc_info=True,
            )
        return
    try:
        bus = get_event_bus()
        await bus.publish(
            Event(
                kind=AUDIT_EMIT_KIND,
                payload={"event": event, "session": session},
            )
        )
    except BaseException:  # noqa: BLE001 — last-resort guard, never propagate
        logger.warning(
            "supervisor_audit_publish_failed",
            event_type=getattr(event, "event_type", None),
            exc_info=True,
        )
