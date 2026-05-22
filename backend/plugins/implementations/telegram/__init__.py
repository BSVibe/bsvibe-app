"""Telegram connector plugin (Workflow §6 #4 capability model).

Capabilities:

* ``@p.inbound`` — parse a Telegram Bot-API webhook Update (a plain
  ``message``) into a :class:`backend.intake.schema.TriggerEvent`, verifying
  Telegram's secret-token scheme (the ``X-Telegram-Bot-Api-Secret-Token``
  header must equal the secret configured via ``setWebhook``, constant-time
  compare) and using the Telegram ``update_id`` as the idempotency key.
* ``@p.outbound(artifact_types=["telegram_message"])`` — send a message
  (``sendMessage``).
* ``@p.compensate`` — delete the message (T2, trail — ``deleteMessage`` works
  only within 48h and a recipient may already have seen it), idempotent.
* ``@p.action`` — ``send_message`` exposed as an agent-loop tool.
* ``@p.setup`` — bot-token / webhook-secret credential flow.

All external I/O goes through :class:`~.client.TelegramClient` (httpx); tests
mock httpx and never reach real Telegram. Telegram returns HTTP 200 with
``{"ok": false}`` on logical failure — the client handles that explicitly.
"""

from __future__ import annotations
