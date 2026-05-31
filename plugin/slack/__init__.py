"""Slack connector plugin (Workflow §6 #4 capability model).

Capabilities:

* ``@p.inbound`` — parse a Slack Events-API delivery (app_mention / message)
  into a :class:`backend.workflow.domain.incoming.TriggerEvent`, with signing-secret
  HMAC-SHA256 verification (+ five-minute replay window) and the Slack
  ``event_id`` as the idempotency key.
* ``@p.outbound(artifact_types=["slack_message"])`` — post a chat message
  (``chat.postMessage``).
* ``@p.compensate`` — delete the message (T2, trail — ``chat.delete`` leaves
  an audit record), idempotent.
* ``@p.action`` — ``post_message`` exposed as an agent-loop tool.
* ``@p.setup`` — bot-token / signing-secret credential flow.

All external I/O goes through :class:`~.client.SlackClient` (httpx); tests
mock httpx and never reach real Slack. Slack returns HTTP 200 with
``{"ok": false}`` on logical failure — the client handles that explicitly.
"""

from __future__ import annotations
