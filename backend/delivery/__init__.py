"""Delivery — Workflow §12.5 #8 (Bundle G).

Outbound surface: aggregates :class:`ActionResult` from plugin
outbound adapters into a :class:`DeliveryResult`, gated by the
:class:`SafeModeQueue` when the workspace is in Safe Mode.
"""

from __future__ import annotations

from backend.delivery.db import (
    DeliveryBase,
    DeliveryEventRow,
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from backend.delivery.dispatcher import DeliveryDispatcher
from backend.delivery.safe_mode_queue import SafeModeQueue
from backend.delivery.schema import (
    ActionResult,
    ArtifactType,
    DeliveryResult,
)

__all__ = [
    "ActionResult",
    "ArtifactType",
    "DeliveryBase",
    "DeliveryDispatcher",
    "DeliveryEventRow",
    "DeliveryResult",
    "SafeModeQueue",
    "SafeModeQueueItemRow",
    "SafeModeStatus",
]
