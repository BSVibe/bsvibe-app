"""Telegram webhook parsing → :class:`backend.workflow.domain.incoming.TriggerEvent`.

Pure functions (no I/O) so they unit-test without httpx. The inbound
capability in :mod:`plugin.telegram.plugin` wires the
configured ``webhook_secret`` into :func:`parse_update`.

Security: Telegram's webhook auth is the **secret-token** scheme. When you call
``setWebhook`` you pass a ``secret_token``; Telegram then echoes it back on
every delivery in the ``X-Telegram-Bot-Api-Secret-Token`` header. When a secret
is configured here, that header must equal the secret — compared constant-time
with :func:`hmac.compare_digest` — before any payload is trusted. (Telegram does
not sign the body, so there is no HMAC-over-body / replay-window step like
Slack's; the shared secret token is the whole scheme.)
"""

from __future__ import annotations

import hmac
import json
import uuid
from typing import Any

import structlog

from backend.workflow.domain.incoming import TriggerEvent
from bsvibe_sdk import WebhookError as _SdkWebhookError
from bsvibe_sdk import WebhookSignatureError as _SdkWebhookSignatureError
from bsvibe_sdk import webhook

logger = structlog.get_logger(__name__)

# Telegram delivers the configured secret in this header on every update.
# (Header *name*, not a secret value — S105 is a false positive here.)
SECRET_TOKEN_HEADER = "x-telegram-bot-api-secret-token"  # noqa: S105

# Top-level Update fields this connector turns into TriggerEvents. Anything else
# (edited_message, channel_post, callback_query, inline_query, ...) is skipped.
SUPPORTED_UPDATE_KEYS = frozenset({"message"})


class WebhookError(_SdkWebhookError):
    """Raised when an update cannot be parsed (malformed / missing fields)."""


class WebhookSignatureError(_SdkWebhookSignatureError, WebhookError):
    """Raised when secret-token verification fails — treat as forged."""


def verify_secret_token(secret: str, token_header: str | None) -> None:
    """Raise :class:`WebhookSignatureError` unless the header equals the secret.

    Implements Telegram's secret-token scheme: the value in
    ``X-Telegram-Bot-Api-Secret-Token`` must match the secret configured via
    ``setWebhook``. Compared constant-time to avoid leaking the secret through
    timing.
    """
    if not token_header:
        raise WebhookSignatureError("missing X-Telegram-Bot-Api-Secret-Token header")
    if not hmac.compare_digest(secret, token_header):
        raise WebhookSignatureError("secret token mismatch")


def _lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


@webhook("telegram")
def parse_update(
    *,
    workspace_id: uuid.UUID,
    headers: dict[str, str],
    raw_body: bytes,
    secret: str | None = None,
) -> TriggerEvent | None:
    """Parse one Telegram webhook Update delivery.

    Returns a :class:`TriggerEvent` for supported updates (a plain ``message``),
    or ``None`` to skip (non-message updates, bot-authored messages). Raises
    :class:`WebhookSignatureError` when ``secret`` is given and the secret-token
    header does not match, :class:`WebhookError` on malformed input.

    The stable ``idempotency_key`` is derived from the Telegram ``update_id``
    (monotonic per bot), so a redelivered update collapses to the same intake
    row.
    """
    h = _lower_headers(headers)

    if secret is not None:
        verify_secret_token(secret, h.get(SECRET_TOKEN_HEADER))

    try:
        body: dict[str, Any] = json.loads(raw_body)
    except (ValueError, TypeError) as exc:
        raise WebhookError(f"telegram update body is not valid JSON: {exc}") from exc

    update_id = body.get("update_id")
    if update_id is None:
        raise WebhookError("missing update_id")

    # Only plain incoming messages enter the workflow. Everything else
    # (edited_message, channel_post, callback_query, ...) is skipped.
    update_key = next((k for k in SUPPORTED_UPDATE_KEYS if k in body), None)
    if update_key is None:
        logger.debug("telegram_update_skip", keys=sorted(body.keys()))
        return None

    message = body[update_key] or {}

    # Skip bot-authored messages to avoid self-trigger loops.
    if (message.get("from") or {}).get("is_bot"):
        return None

    chat = message.get("chat") or {}
    payload = {
        "telegram_update": update_key,
        "update_id": update_id,
        "chat_id": chat.get("id"),
        "user_id": (message.get("from") or {}).get("id"),
        "text": message.get("text"),
        "message_id": message.get("message_id"),
        "body": body,
    }
    return TriggerEvent(
        workspace_id=workspace_id,
        source="telegram",
        trigger_kind="webhook",
        idempotency_key=f"telegram:{update_id}",
        payload=payload,
        trace_id=str(update_id),
    )


__all__ = [
    "SECRET_TOKEN_HEADER",
    "SUPPORTED_UPDATE_KEYS",
    "WebhookError",
    "WebhookSignatureError",
    "parse_update",
    "verify_secret_token",
]
