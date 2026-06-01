"""Linear connector plugin (Workflow §6 #4 capability model).

Capabilities:

* ``@p.outbound(artifact_types=["issue"])`` — create a Linear issue from a
  deliverable (compensation tier ``t3_new_artifact``).
* ``@p.compensate`` — archive the created issue, idempotent.
* ``@p.action`` — ``create_issue`` exposed as an agent-loop tool.
* ``@p.setup`` — Linear personal API key credential flow.

There is no ``@p.inbound`` capability: Linear inbound webhooks are a later
chunk.

All external I/O goes through :class:`~.client.LinearClient` (httpx, Linear
GraphQL API); tests mock httpx and never reach real Linear.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
