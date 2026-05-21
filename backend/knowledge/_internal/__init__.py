"""Knowledge-internal shared infrastructure.

Lifted from ``bsage.core`` to keep the knowledge module self-contained. If
another backend module later needs the same primitives, these can be promoted
to ``backend.shared.core``.
"""

from __future__ import annotations

from backend.knowledge._internal.events import (
    Event,
    EventBus,
    EventEmitterAdapter,
    EventSubscriber,
    EventType,
    emit_event,
)
from backend.knowledge._internal.exceptions import (
    KnowledgeError,
    SafeModeError,
    VaultPathError,
)
from backend.knowledge._internal.patterns import WIKILINK_RE
from backend.knowledge._internal.protocols import ContextBuilderLike, RunnerLike
from backend.knowledge._internal.tasks import spawn_task

__all__ = [
    "WIKILINK_RE",
    "ContextBuilderLike",
    "Event",
    "EventBus",
    "EventEmitterAdapter",
    "EventSubscriber",
    "EventType",
    "KnowledgeError",
    "RunnerLike",
    "SafeModeError",
    "VaultPathError",
    "emit_event",
    "spawn_task",
]
