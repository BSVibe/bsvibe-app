"""GitHub webhook parsing → :class:`backend.workflow.domain.incoming.TriggerEvent`.

Pure functions (no I/O) so they unit-test without httpx. The inbound
capability in :mod:`plugin.github.plugin` wires the
configured ``webhook_secret`` into :func:`parse_webhook`.

Security: when a secret is configured, the raw request body is HMAC-SHA256
verified against the ``X-Hub-Signature-256`` header (GitHub's scheme) using a
constant-time compare before any payload is trusted.

Lift Q3 / R2c — :func:`parse_webhook` is marked with the SDK
``@webhook("github")`` decorator so the engine's PluginLoader registers
it with the :class:`WebhookParserRegistry`; ``backend.connectors.resolver``
dispatches via that registry instead of importing this module directly.
``WebhookSignatureError`` extends the SDK base so a single
``except bsvibe_sdk.WebhookSignatureError`` covers every connector's
forgery flavour.
"""

from __future__ import annotations

import hashlib
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


class WebhookError(_SdkWebhookError):
    """Raised when a webhook cannot be parsed (malformed / missing headers)."""


class WebhookSignatureError(_SdkWebhookSignatureError, WebhookError):
    """Raised when HMAC signature verification fails — treat as forged."""


# GitHub event types this connector turns into TriggerEvents. Anything else
# (push, star, ping, ...) is intentionally skipped (returns None).
SUPPORTED_EVENTS = frozenset({"issues", "pull_request", "issue_comment"})

# Per-event actions worth entering the workflow for. Other actions
# (closed, labeled, assigned, ...) are skipped.
ACTED_ACTIONS: dict[str, frozenset[str]] = {
    "issues": frozenset({"opened", "edited", "reopened"}),
    "pull_request": frozenset({"opened", "edited", "reopened", "ready_for_review", "synchronize"}),
    "issue_comment": frozenset({"created", "edited"}),
}


def verify_signature(secret: str, raw_body: bytes, signature_header: str | None) -> None:
    """Raise :class:`WebhookSignatureError` unless the signature matches.

    GitHub sends ``X-Hub-Signature-256: sha256=<hexdigest>`` — HMAC-SHA256 of
    the raw request body keyed by the webhook secret.
    """
    if not signature_header:
        raise WebhookSignatureError("missing X-Hub-Signature-256 header")
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise WebhookSignatureError("signature mismatch")


def _lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


@webhook("github")
def parse_webhook(
    *,
    workspace_id: uuid.UUID,
    headers: dict[str, str],
    raw_body: bytes,
    secret: str | None = None,
) -> TriggerEvent | None:
    """Parse one GitHub webhook delivery.

    Returns a :class:`TriggerEvent` for supported event+action pairs, or
    ``None`` to skip (ping, unsupported event, uninteresting action, bot
    sender). Raises :class:`WebhookSignatureError` when ``secret`` is given
    and verification fails, :class:`WebhookError` on malformed input.
    """
    h = _lower_headers(headers)
    event = h.get("x-github-event")
    delivery = h.get("x-github-delivery")

    if secret is not None:
        verify_signature(secret, raw_body, h.get("x-hub-signature-256"))

    if event in (None, "ping"):
        return None
    if event not in SUPPORTED_EVENTS:
        logger.debug("github_webhook_skip_event", gh_event=event)
        return None

    try:
        body: dict[str, Any] = json.loads(raw_body)
    except (ValueError, TypeError) as exc:
        raise WebhookError(f"github webhook body is not valid JSON: {exc}") from exc

    action = body.get("action")
    if action is not None and action not in ACTED_ACTIONS.get(event, frozenset()):
        logger.debug("github_webhook_skip_action", gh_event=event, action=action)
        return None

    # Skip bot-authored events to avoid self-trigger loops.
    if (body.get("sender") or {}).get("type") == "Bot":
        return None

    if not delivery:
        raise WebhookError("missing X-GitHub-Delivery header")

    repo = (body.get("repository") or {}).get("full_name")
    # The framer reads ``intent_text`` as the work directive. Pull the human text
    # of the issue / PR / comment so a github-triggered run knows WHAT to do —
    # without it the run has no instruction and degrades to a generic
    # "how can I help?" answer instead of doing the work.
    intent_text = _intent_text(event, body)
    payload = {
        "github_event": event,
        "action": action,
        "repo": repo,
        "delivery": delivery,
        "intent_text": intent_text,
        "body": body,
    }
    return TriggerEvent(
        workspace_id=workspace_id,
        source="github",
        trigger_kind="webhook",
        idempotency_key=delivery,
        payload=payload,
        trace_id=delivery,
        intent_text=intent_text,
    )


def _join_text(parts: list[Any]) -> str | None:
    """Join non-empty parts with a blank line, or ``None`` when all are empty."""
    cleaned = [str(p).strip() for p in parts if p is not None and str(p).strip()]
    return "\n\n".join(cleaned) or None


def _intent_text(event: str, body: dict[str, Any]) -> str | None:
    """The work directive — the human text of the triggering issue / PR / comment."""
    if event == "issues":
        issue = body.get("issue") or {}
        return _join_text([issue.get("title"), issue.get("body")])
    if event == "pull_request":
        pr = body.get("pull_request") or {}
        return _join_text([pr.get("title"), pr.get("body")])
    if event == "issue_comment":
        # The comment is the new instruction; prefix the issue title for context.
        issue = body.get("issue") or {}
        comment = body.get("comment") or {}
        return _join_text([issue.get("title"), comment.get("body")])
    return None


__all__ = [
    "ACTED_ACTIONS",
    "SUPPORTED_EVENTS",
    "WebhookError",
    "WebhookSignatureError",
    "parse_webhook",
    "verify_signature",
]
