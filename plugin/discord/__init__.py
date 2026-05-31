"""Discord connector plugin (Workflow §6 #4 capability model).

Capabilities:

* ``@p.inbound`` — parse a Discord **interaction** webhook into a
  :class:`backend.workflow.domain.incoming.TriggerEvent`, verifying Discord's
  **Ed25519** request-signing scheme (``X-Signature-Ed25519`` over
  ``X-Signature-Timestamp + raw_body``, public key supplied as the
  ``public_key`` credential) and using the interaction ``id`` as the
  idempotency key. The PING handshake (``type=1``) passes verification but
  returns ``None`` — it is answered by the HTTP route (later chunk).
* ``@p.outbound(artifact_types=["discord_message"])`` — post a channel
  message (``POST /channels/{id}/messages``).
* ``@p.compensate`` — delete the message (T2, trail — ``DELETE`` removes the
  message but a recipient may already have seen it), idempotent.
* ``@p.action`` — ``send_message`` exposed as an agent-loop tool.
* ``@p.setup`` — bot-token / public-key credential flow.

All external I/O goes through :class:`~.client.DiscordClient` (httpx); tests
mock httpx and never reach real Discord. Discord signals API failures with a
non-2xx HTTP status (unlike Slack/Telegram's HTTP-200 ``ok:false``) — the
client raises :class:`~.client.DiscordApiError` on non-2xx, treating
``404 Not Found`` on delete as an idempotent no-op.
"""

from __future__ import annotations
