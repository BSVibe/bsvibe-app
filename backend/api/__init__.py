"""HTTP surface — FastAPI app factory + routers + middleware.

Contract (Lift N-Coverage pattern #8):

* **Owns** the public HTTP edge — the FastAPI app factory, every REST
  route, webhook receivers, request/response middleware, and the
  ``Depends(...)`` graph that wires authenticated requests into the
  bounded contexts.
* **Facade**: there is none — ``api/`` IS the externally visible facade
  for browsers + CLI + MCP clients. Internal callers must NOT import
  from ``backend.api``; they go through the per-context application
  layer instead.
* **Not exposed**: route handlers, middleware classes, and dependency
  factories are private to ``api/`` — internals are not re-exported
  at this namespace.

Namespace-only — concrete entry points live under ``backend.api.main``
(app factory), ``backend.api.v1`` (REST routes), ``backend.api.webhooks``
(connector inbound), ``backend.api.middleware`` and ``backend.api.deps``.
Lift N defensive pattern #1.
"""

from __future__ import annotations

__all__: list[str] = []
