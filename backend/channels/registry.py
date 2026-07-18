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
from backend.workflow.channels import REQUESTS, TRIGGER_EVENTS

ALL_CHANNELS: tuple[Channel[Any], ...] = (TRIGGER_EVENTS, REQUESTS)

__all__ = ["ALL_CHANNELS"]
