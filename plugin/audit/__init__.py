"""BSVibe audit plugin — repo-root location since Lift R2a.

History: lifted from the external ``bsvibe_audit`` library + BSupervisor's
``core/audit.py`` wrapper (events / emitter / outbox + soft-fail emit).
Relocated to ``backend/supervisor/audit/`` then to
``backend/extensions/implementations/audit/`` in Lift G and finally to
repo-root ``plugin/audit/`` in Lift R2a alongside the EventBus rewire.

Per v8 §D5 audit is now a first-class plugin: producers no longer import
``safe_emit`` to talk to a direct emitter — ``safe_emit`` publishes an
``audit.emit`` :class:`bsvibe_sdk.Event` and the
:class:`AuditEventSubscriber` registered on this module's import persists
the carried event to ``audit_outbox`` inside the producer's session
(synchronous in-process bus — transactional outbox semantics preserved).

Public surface::

    from plugin.audit import (
        AuditActor, AuditEvent, AuditEventBase, AuditResource,
        AuditEmitter, AuditOutboxRecord, OutboxStore,
        AuditEventSubscriber,
        safe_emit, make_actor,
    )

The relay loop (the half that ships outbox rows to BSVibe-Auth) still
lives in :mod:`backend.workers.relay_worker`. ``OutboxStore.select_undelivered``
is the seam.
"""

from __future__ import annotations

from backend.extensions.eventbus import get_event_bus
from plugin.audit.emitter import AuditEmitter
from plugin.audit.events import (
    ActorType,
    AuditActor,
    AuditEventBase,
    AuditResource,
)
from plugin.audit.models import (
    AuditEvent,
    AuditOutboxBase,
    AuditOutboxRecord,
    SupervisorBase,
)
from plugin.audit.service import make_actor, safe_emit
from plugin.audit.store import OutboxStore
from plugin.audit.subscriber import (
    AUDIT_EMIT_KIND,
    AUDIT_KIND_PREFIX,
    AuditEventSubscriber,
)

# Lift R2a — register the audit subscriber on the in-process bus singleton
# on first import. Guarded so re-import (e.g. test reload) does not
# duplicate the subscription.
_REGISTERED = False
_SUBSCRIBER: AuditEventSubscriber | None = None


def _register_subscriber() -> None:
    global _REGISTERED, _SUBSCRIBER  # noqa: PLW0603 — import-time singleton wiring
    if _REGISTERED:
        return
    bus = get_event_bus()
    if AUDIT_KIND_PREFIX in bus.registered_prefixes():
        # Already wired (singleton survived across imports).
        _REGISTERED = True
        return
    _SUBSCRIBER = AuditEventSubscriber()
    bus.subscribe(AUDIT_KIND_PREFIX, _SUBSCRIBER)
    _REGISTERED = True


_register_subscriber()


__all__ = [
    "AUDIT_EMIT_KIND",
    "AUDIT_KIND_PREFIX",
    "ActorType",
    "AuditActor",
    "AuditEmitter",
    "AuditEvent",
    "AuditEventBase",
    "AuditEventSubscriber",
    "AuditOutboxBase",
    "AuditOutboxRecord",
    "AuditResource",
    "OutboxStore",
    "SupervisorBase",
    "make_actor",
    "safe_emit",
]
