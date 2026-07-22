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
import urllib.parse
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

    # Interactivity POSTs (a founder tapping 승인 / 거절 on a Block Kit card) arrive
    # form-encoded as ``payload=<url-encoded JSON>`` (NOT raw JSON) with the SAME
    # signature scheme (verified above, verbatim over the raw body). They are a
    # SYNCHRONOUS approve/reject action, NOT a new run, so they stay OUT of intake:
    # return None here (mirroring the telegram callback_query skip) so the webhook
    # route's ``event is None`` branch hands off to the interaction callback.
    if decode_interaction_payload(raw_body) is not None:
        return None

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


# block_actions button verbs an approve/reject tap can carry (mirror
# ``backend.notifications.notify_builders`` CALLBACK_APPROVE / CALLBACK_REJECT).
_INTERACTION_VERBS = frozenset({"apv", "rej"})


def decode_interaction_payload(raw_body: bytes) -> dict[str, Any] | None:
    """Decode a Slack interactivity POST body → its ``block_actions`` payload dict.

    Slack POSTs interactions form-encoded as ``payload=<url-encoded JSON>``. Return
    the decoded payload dict when it is a ``block_actions`` interaction, else
    ``None`` (a normal Events-API delivery is raw JSON with no ``payload`` field,
    an unparseable / non-block_actions interaction is not ours to handle). Pure —
    no signature check (the caller already verified the raw body)."""
    try:
        text = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else str(raw_body)
    except UnicodeDecodeError:
        return None
    fields = urllib.parse.parse_qs(text)
    payloads = fields.get("payload")
    if not payloads:
        return None
    try:
        data: Any = json.loads(payloads[0])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("type") != "block_actions":
        return None
    return data


def parse_interaction(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a ``block_actions`` payload into founder-auth + action fields.

    Pure (no I/O — the request was already gated by the webhook_token + signature).
    Returns the fields the inbound callback handler needs::

        {verb, deliverable_id, user_id, team_id, channel_id, message_ts,
         message_blocks, response_url, malformed}

    ``verb`` / ``deliverable_id`` are parsed from ``actions[0].value`` (falling
    back to ``action_id``) as ``"<verb>:<deliverable_id>"``; they are ``None`` and
    ``malformed`` is ``True`` when the verb is not in {apv, rej} or the id is
    absent. ``message_blocks`` is the ORIGINAL card's blocks — needed to keep the
    body when the handler edits the message on settle."""
    actions = payload.get("actions") or []
    action = actions[0] if actions and isinstance(actions[0], dict) else {}
    raw = str(action.get("value") or action.get("action_id") or "")
    verb, _, deliverable_id = raw.partition(":")
    malformed = verb not in _INTERACTION_VERBS or not deliverable_id
    user = payload.get("user") or {}
    team = payload.get("team") or {}
    channel = payload.get("channel") or {}
    message = payload.get("message") or {}
    return {
        "verb": None if malformed else verb,
        "deliverable_id": None if malformed else deliverable_id,
        "user_id": user.get("id"),
        # A block_actions payload carries the team at ``team.id``; fall back to the
        # user's ``team_id`` (present on some interaction shapes).
        "team_id": team.get("id") or user.get("team_id"),
        "channel_id": channel.get("id"),
        "message_ts": message.get("ts"),
        "message_blocks": message.get("blocks"),
        "response_url": payload.get("response_url"),
        "malformed": malformed,
    }


__all__ = [
    "MAX_TIMESTAMP_SKEW",
    "SUPPORTED_EVENTS",
    "WebhookError",
    "WebhookSignatureError",
    "decode_interaction_payload",
    "parse_event",
    "parse_interaction",
    "verify_signature",
]
