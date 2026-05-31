"""Router context (Lift A — facade only, no concrete impl yet).

Re-exports the Protocol + dataclasses defined in :mod:`backend.router.facade`.
Subsequent lifts wire the existing ``backend.gateway`` + ``backend.routing`` +
``backend.executors`` code behind this facade.
"""

from __future__ import annotations

from backend.router.facade import (
    LlmRequest,
    LlmResult,
    LlmRoutingHints,
    Router,
)

__all__ = ["LlmRequest", "LlmResult", "LlmRoutingHints", "Router"]
