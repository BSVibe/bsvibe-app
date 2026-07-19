"""Channel registry (INV-1) — the enumeration of every declared channel.

This module imports each per-context channel declaration **only for
enumeration** (the meta-tests + any future catalog). It may reach across
contexts because it is tooling/test-facing, like the architecture meta-tests
themselves — it is not a common leaf and nothing on a worker's hot path
imports it. Producers/consumers import their **own** context's channel
module, never this registry.
"""

from __future__ import annotations

from typing import Any

from backend.channels._core import Channel
from backend.channels._events import EventChannel
from backend.notifications.channels import NOTIFICATION_OUTBOX
from backend.schedule.channels import WORKSPACE_SCHEDULES
from backend.workflow.channels import (
    DELIVERY_EVENTS,
    REQUESTS,
    SAFE_MODE_QUEUE_ITEMS,
    TRIGGER_EVENTS,
)
from plugin.audit.channels import AUDIT_EMIT, AUDIT_OUTBOX

ALL_CHANNELS: tuple[Channel[Any], ...] = (
    WORKSPACE_SCHEDULES,
    TRIGGER_EVENTS,
    REQUESTS,
    SAFE_MODE_QUEUE_ITEMS,
    DELIVERY_EVENTS,
    AUDIT_OUTBOX,
    NOTIFICATION_OUTBOX,
)

# EventChannels are a SIBLING type (in-process bus topics), enumerated
# separately from the durable-row ``ALL_CHANNELS`` above.
ALL_EVENT_CHANNELS: tuple[EventChannel[Any], ...] = (AUDIT_EMIT,)

__all__ = ["ALL_CHANNELS", "ALL_EVENT_CHANNELS"]
