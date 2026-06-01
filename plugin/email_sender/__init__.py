"""Email-sender connector plugin (Workflow §6 #4 capability model).

Capabilities:

* ``@p.outbound(artifact_types=["email"])`` — send a transactional email from a
  deliverable via the Resend API (compensation tier ``t4_irreversible``).
* ``@p.compensate`` — a sent email cannot be recalled; the handler records that
  no clean undo exists (notify-style no-op, idempotent).
* ``@p.action`` — ``send_email`` exposed as an agent-loop tool.
* ``@p.setup`` — Resend API key + default ``from`` address credential flow.

There is no ``@p.inbound`` capability: this connector is **outbound-only**.

All external I/O goes through :class:`~.client.ResendClient` (httpx); tests
mock httpx and never reach real Resend.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
