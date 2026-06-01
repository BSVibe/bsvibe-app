"""HTTP surface — FastAPI app factory + routers + middleware.

Namespace-only — concrete entry points live under ``backend.api.main``
(app factory), ``backend.api.v1`` (REST routes), ``backend.api.webhooks``
(connector inbound), ``backend.api.middleware`` and ``backend.api.deps``.
Lift N defensive pattern #1.
"""

from __future__ import annotations

__all__: list[str] = []
