"""Channel abstraction (INV-1) — public surface.

Producers and consumers import the reusable :class:`Channel` type from here
(or declare their channel next to their rows using it). This package root is
deliberately **context-free**: it re-exports only the generic core so that a
per-context channel module can import it without dragging any bounded context
into a producer's hot path.

The cross-context enumeration (``ALL_CHANNELS``) lives in
:mod:`backend.channels.registry`, not here — importing it would couple this
root to every context and create an import cycle with the per-context
declarations. Meta-tests and future catalog tooling import the registry
directly.
"""

from __future__ import annotations

from backend.channels._core import (
    Channel,
    SupportsAdd,
    UndeclaredConsumerError,
    UndeclaredProducerError,
)

__all__ = [
    "Channel",
    "SupportsAdd",
    "UndeclaredConsumerError",
    "UndeclaredProducerError",
]
