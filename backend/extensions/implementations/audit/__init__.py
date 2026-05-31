"""BSVibe audit log — first extension implementation (Lift G).

Folded from the external ``bsvibe_audit`` library (events / emitter /
outbox) plus the ``BSupervisor/bsupervisor/core/audit.py`` wrapper that
made emits *never* break a domain write. Relocated from
``backend/supervisor/audit/`` in Lift G as the first dogfood of the new
extension implementation layout. Lift R relocates again to repo-root
``plugin/audit/`` after the ``bsvibe_sdk`` lands in Lift S.

Public surface::

    from backend.extensions.implementations.audit import (
        AuditActor, AuditEvent, AuditEventBase, AuditResource,
        AuditEmitter, AuditOutboxRecord, OutboxStore,
        safe_emit, make_actor,
    )

The relay loop (the half that ships outbox rows to BSVibe-Auth) is not
included in this bundle — it depends on the orchestrator/workers track
landing first. ``OutboxStore.select_undelivered`` is provided so a later
bundle can wire it up.

NOTE: audit becomes a formal ``EventBusSubscriber`` (see
``backend.extensions.domain.protocols``) in Lift I or N — Lift G keeps
the existing direct-emit shape; the Protocol is published but unwired.
"""

from __future__ import annotations

from backend.extensions.implementations.audit.emitter import AuditEmitter
from backend.extensions.implementations.audit.events import (
    ActorType,
    AuditActor,
    AuditEventBase,
    AuditResource,
)
from backend.extensions.implementations.audit.models import (
    AuditEvent,
    AuditOutboxBase,
    AuditOutboxRecord,
    SupervisorBase,
)
from backend.extensions.implementations.audit.service import make_actor, safe_emit
from backend.extensions.implementations.audit.store import OutboxStore

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
