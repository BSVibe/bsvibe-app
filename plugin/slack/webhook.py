"""Slack event parsing → :class:`backend.workflow.domain.incoming.TriggerEvent`.

Pure functions (no I/O) so they unit-test without httpx. The inbound
capability in :mod:`plugin.slack.plugin` wires the
configured ``signing_secret`` into :func:`parse_event`.

Security: when a secret is configured, the raw request body is verified with
Slack's signing-secret scheme before any payload is trusted::

    base   = "v0:" + X-Slack-Request-Timestamp + ":" + raw_body
    sig    = "v0=" + HMAC_SHA256(signing_secret, base)
    compare(sig, X-Slack-Signature)   # constant-time

The timestamp is additionally checked against a five-minute replay window to
defeat captured-request replay (Slack's recommendation).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import structlog

from backend.workflow.domain.incoming import TriggerEvent
from bsvibe_sdk import WebhookError as _SdkWebhookError
from bsvibe_sdk import WebhookSignatureError as _SdkWebhookSignatureError
from bsvibe_sdk import webhook

logger = structlog.get_logger(__name__)

# Reject any request whose timestamp is older than this (seconds) — replay guard.
MAX_TIMESTAMP_SKEW = 60 * 5


class WebhookError(_SdkWebhookError):
    """Raised when an event cannot be parsed (malformed / missing fields)."""


class WebhookSignatureError(_SdkWebhookSignatureError, WebhookError):
    """Raised when signing-secret verification fails — treat as forged."""


# Slack inner event types this connector turns into TriggerEvents. Anything
# else (reaction_added, member_joined_channel, ...) is skipped (returns None).
SUPPORTED_EVENTS = frozenset({"app_mention", "message"})


def verify_signature(
    secret: str,
    raw_body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
) -> None:
    """Raise :class:`WebhookSignatureError` unless the signature matches.

    Implements Slack's signing-secret scheme: HMAC-SHA256 over
    ``"v0:" + timestamp + ":" + raw_body`` keyed by the signing secret,
    prefixed ``v0=`` and compared constant-time against ``X-Slack-Signature``.
    A timestamp older than :data:`MAX_TIMESTAMP_SKEW` is rejected (replay).
    """
    if not signature_header:
        raise WebhookSignatureError("missing X-Slack-Signature header")
    if not timestamp_header:
        raise WebhookSignatureError("missing X-Slack-Request-Timestamp header")
    try:
        ts = int(timestamp_header)
    except (TypeError, ValueError) as exc:
        raise WebhookSignatureError(f"invalid X-Slack-Request-Timestamp: {exc}") from exc
    if abs(time.time() - ts) > MAX_TIMESTAMP_SKEW:
        raise WebhookSignatureError("stale X-Slack-Request-Timestamp (possible replay)")
    base = b"v0:" + timestamp_header.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise WebhookSignatureError("signature mismatch")


def _lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


@webhook("slack")
def parse_event(
    *,
    workspace_id: uuid.UUID,
    headers: dict[str, str],
    raw_body: bytes,
    secret: str | None = None,
) -> TriggerEvent | None:
    """Parse one Slack Events-API delivery.

    Returns a :class:`TriggerEvent` for supported inner events, or ``None`` to
    skip (the ``url_verification`` handshake, unsupported event types, bot
    authors). Raises :class:`WebhookSignatureError` when ``secret`` is given
    and verification fails, :class:`WebhookError` on malformed input.
    """
    h = _lower_headers(headers)

    if secret is not None:
        verify_signature(
            secret,
            raw_body,
            h.get("x-slack-signature"),
            h.get("x-slack-request-timestamp"),
        )

    try:
        body: dict[str, Any] = json.loads(raw_body)
    except (ValueError, TypeError) as exc:
        raise WebhookError(f"slack event body is not valid JSON: {exc}") from exc

    envelope_type = body.get("type")
    # The one-time URL verification handshake is answered by the HTTP route,
    # not the workflow — skip it here.
    if envelope_type == "url_verification":
        return None
    if envelope_type != "event_callback":
        logger.debug("slack_event_skip_envelope", envelope=envelope_type)
        return None

    inner = body.get("event") or {}
    event_type = inner.get("type")
    if event_type not in SUPPORTED_EVENTS:
        logger.debug("slack_event_skip_type", slack_event=event_type)
        return None

    # Skip bot-authored messages to avoid self-trigger loops.
    if inner.get("bot_id"):
        return None

    event_id = body.get("event_id")
    if not event_id:
        raise WebhookError("missing event_id")

    payload = {
        "slack_event": event_type,
        "channel": inner.get("channel"),
        "user": inner.get("user"),
        "text": inner.get("text"),
        "event_ts": inner.get("ts"),
        "team_id": body.get("team_id"),
        "event_id": event_id,
        "body": body,
    }
    return TriggerEvent(
        workspace_id=workspace_id,
        source="slack",
        trigger_kind="webhook",
        idempotency_key=event_id,
        payload=payload,
        trace_id=event_id,
    )


__all__ = [
    "MAX_TIMESTAMP_SKEW",
    "SUPPORTED_EVENTS",
    "WebhookError",
    "WebhookSignatureError",
    "parse_event",
    "verify_signature",
]
