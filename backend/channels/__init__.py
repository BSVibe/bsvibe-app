"""Channel abstraction (INV-1) — public surface.

Producers and consumers import the reusable :class:`Channel` (durable row
seam) or its sibling :class:`EventChannel` (in-process bus topic) from here —
or declare their channel next to their rows/kinds using them. This package
root is deliberately **context-free**: it re-exports only the generic cores so
that a per-context channel module can import it without dragging any bounded
context into a producer's hot path.

The cross-context enumerations (``ALL_CHANNELS`` for rows, ``ALL_EVENT_CHANNELS``
for bus topics) live in :mod:`backend.channels.registry`, not here — importing
them would couple this root to every context and create an import cycle with the
per-context declarations. Meta-tests and future catalog tooling import the
registry directly.
"""

from __future__ import annotations

from backend.channels._core import (
    Channel,
    SupportsAdd,
    UndeclaredConsumerError,
    UndeclaredProducerError,
)
from backend.channels._events import (
    Delivered,
    EventChannel,
    NoSubscriber,
    PublishOutcome,
    SubscriberRaised,
    SupportsPublish,
    SupportsSubscribe,
    UndeclaredPublisherError,
    UndeclaredSubscriberError,
)

__all__ = [
    "Channel",
    "Delivered",
    "EventChannel",
    "NoSubscriber",
    "PublishOutcome",
    "SubscriberRaised",
    "SupportsAdd",
    "SupportsPublish",
    "SupportsSubscribe",
    "UndeclaredConsumerError",
    "UndeclaredProducerError",
    "UndeclaredPublisherError",
    "UndeclaredSubscriberError",
]
