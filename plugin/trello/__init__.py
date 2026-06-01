"""Trello connector plugin (Workflow §6 #4 capability model).

Capabilities:

* ``@p.outbound(artifact_types=["card"])`` — create a Trello card from a
  deliverable (compensation tier ``t3_new_artifact``).
* ``@p.compensate`` — archive (close) the created card, idempotent.
* ``@p.action`` — ``create_card`` exposed as an agent-loop tool.
* ``@p.setup`` — Trello API key + token credential flow.

There is no ``@p.inbound`` capability: this connector is delivery-only.

All external I/O goes through :class:`~.client.TrelloClient` (httpx, Trello
REST API). Trello auth is query-param based (``?key=...&token=...``), NOT a
Bearer header. Tests mock httpx and never reach real Trello.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
