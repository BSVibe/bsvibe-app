"""BSVibe supervisor audit log.

Folded from the external ``bsvibe_audit`` library (events / emitter /
outbox) plus the ``BSupervisor/bsupervisor/core/audit.py`` wrapper that
made emits *never* break a domain write.

Public surface::

    from backend.supervisor.audit import (
        AuditActor, AuditEvent, AuditEventBase, AuditResource,
        AuditEmitter, AuditOutboxRecord, OutboxStore,
        safe_emit, make_actor,
    )

The relay loop (the half that ships outbox rows to BSVibe-Auth) is not
included in this bundle — it depends on the orchestrator/workers track
landing first. ``OutboxStore.select_undelivered`` is provided so a later
bundle can wire it up.
"""

from __future__ import annotations

from backend.supervisor.audit.emitter import AuditEmitter
from backend.supervisor.audit.events import (
    ActorType,
    AuditActor,
    AuditEventBase,
    AuditResource,
)
from backend.supervisor.audit.models import (
    AuditEvent,
    AuditOutboxBase,
    AuditOutboxRecord,
    SupervisorBase,
)
from backend.supervisor.audit.service import make_actor, safe_emit
from backend.supervisor.audit.store import OutboxStore

__all__ = [
    "ActorType",
    "AuditActor",
    "AuditEmitter",
    "AuditEvent",
    "AuditEventBase",
    "AuditOutboxBase",
    "AuditOutboxRecord",
    "AuditResource",
    "OutboxStore",
    "SupervisorBase",
    "make_actor",
    "safe_emit",
]
