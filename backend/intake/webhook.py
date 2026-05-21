"""WebhookReceiver — adapt inbound HTTP/webhook payloads into TriggerEvent.

Workflow §12.5 #8 (Bundle G — Intake / Triggers). The concrete lift lands
in a follow-up commit; this skeleton fixes the public contract so
upstream API routers can wire against it now.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from backend.intake.schema import TriggerEvent

logger = structlog.get_logger(__name__)


class WebhookReceiver:
    """Adapt an inbound plugin webhook into a :class:`TriggerEvent`.

    The receiver is responsible for:

    * Verifying the plugin's HMAC / signature header (delegated to the
      plugin's adapter — see Workflow §6 #2).
    * Deriving a stable ``idempotency_key`` from the payload (each
      plugin gets to declare its own key extraction strategy).
    * Producing the TriggerEvent envelope.
    """

    async def handle(
        self,
        *,
        workspace_id: uuid.UUID,
        source: str,
        headers: dict[str, str],
        body: dict[str, Any],
    ) -> TriggerEvent:
        """Adapt one inbound webhook delivery into a TriggerEvent."""
        # TODO(bundle-g-integration): concrete lift from BSNexus
        # backend/api/webhooks.py + per-plugin adapter. Steps:
        #   1. resolve plugin adapter via backend.plugins registry
        #   2. adapter.verify_signature(headers, body)
        #   3. adapter.extract_idempotency_key(body)
        #   4. backend.intake.idempotency.is_duplicate(...) early-return
        #   5. record + return TriggerEvent
        logger.debug(
            "webhook_receiver_stub",
            workspace_id=str(workspace_id),
            source=source,
        )
        raise NotImplementedError("WebhookReceiver.handle pending Bundle G integration")


__all__ = ["WebhookReceiver"]
