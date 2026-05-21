"""BSVibe supervisor — audit log + sandbox script execution.

Two role-code modules:

- :mod:`backend.supervisor.audit` — typed audit event emit, in-tx
  outbox, ``safe_emit`` (folded from BSupervisor's ``core/audit.py`` +
  the external ``bsvibe_audit`` library).
- :mod:`backend.supervisor.sandbox` — DinD-backed script runner (lifted
  from BSNexus ``backend/src/core/sandbox/``).

Note: the audit relay loop (outbox → central audit sink) is intentionally
deferred to a follow-up bundle once the orchestrator/workers track lands.
"""

from __future__ import annotations

from backend.supervisor import sandbox
from backend.supervisor.audit import (
    AuditActor,
    AuditEmitter,
    AuditEvent,
    AuditEventBase,
    AuditOutboxRecord,
    AuditResource,
    OutboxStore,
    make_actor,
    safe_emit,
)

__all__ = [
    "AuditActor",
    "AuditEmitter",
    "AuditEvent",
    "AuditEventBase",
    "AuditOutboxRecord",
    "AuditResource",
    "OutboxStore",
    "make_actor",
    "safe_emit",
    "sandbox",
]
