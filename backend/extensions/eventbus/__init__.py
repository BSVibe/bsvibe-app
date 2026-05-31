"""In-process EventBus implementation (Lift R2a).

Concrete :class:`backend.extensions.domain.protocols.EventBus` impl. Until
Lift N introduces a richer transport (Redis Streams / NATS), the bus is a
synchronous prefix-routed in-process dispatcher: ``publish`` awaits every
matching subscriber in registration order so the subscribers' DB writes
land inside the producer's open transaction.

Why synchronous: the audit subscriber persists the event to ``audit_outbox``
through the SAME ``AsyncSession`` the producer is in (transactional outbox
pattern — the row commits / rolls back atomically with the domain write).
A fire-and-forget queue would break that invariant.

Subscriber-side failures NEVER propagate to the producer — the bus catches
and logs, mirroring the pre-R2 ``safe_emit`` swallowing semantics.
"""

from __future__ import annotations

from backend.extensions.eventbus.bus import (
    InProcessEventBus,
    get_event_bus,
    reset_event_bus_for_testing,
)

__all__ = [
    "InProcessEventBus",
    "get_event_bus",
    "reset_event_bus_for_testing",
]
