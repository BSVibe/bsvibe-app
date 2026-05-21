"""Producer-side helpers: ``safe_emit`` + ``make_actor``.

``safe_emit`` swallows emitter failures so the audit infra can never
break a domain write. Lifted from BSupervisor's ``core/audit.py``.
"""

from __future__ import annotations

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


async def safe_emit(
    event: AuditEventBase,
    *,
    session: AsyncSession,
    emitter: AuditEmitter | None = None,
) -> None:
    """Emit an audit event without ever raising into the request handler."""
    try:
        await (emitter or _default_emitter).emit(event, session=session)
    except Exception:  # noqa: BLE001 — audit must never break the domain write
        logger.warning(
            "supervisor_audit_emit_failed",
            event_type=getattr(event, "event_type", None),
            exc_info=True,
        )
