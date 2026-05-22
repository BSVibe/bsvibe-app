"""Notion connector plugin (Workflow §6 #4 capability model).

Capabilities:

* ``@p.outbound(artifact_types=["page", "page_image"])`` — create a Notion page
  from a deliverable (compensation tier ``t3_new_artifact``).
* ``@p.compensate`` — archive (trash) the created page, idempotent.
* ``@p.action`` — ``create_page`` / ``append`` exposed as agent-loop tools.
* ``@p.setup`` — Notion integration token credential flow.

There is no ``@p.inbound`` capability: this connector is delivery-only.

All external I/O goes through :class:`~.client.NotionClient` (httpx); tests
mock httpx and never reach real Notion.
"""

from __future__ import annotations
