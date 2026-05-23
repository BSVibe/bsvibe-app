"""Sentry webhook parsing → :class:`backend.intake.schema.TriggerEvent`.

Pure functions (no I/O) so they unit-test without httpx. The inbound
capability in :mod:`backend.plugins.implementations.sentry.plugin` wires the
configured ``client_secret`` into :func:`parse_webhook`.

Security: when a secret is configured, the raw request body is HMAC-SHA256
verified against the ``Sentry-Hook-Signature`` header (Sentry's scheme — a bare
hex digest, no ``sha256=`` prefix) using a constant-time compare before any
payload is trusted.

Resource routing: Sentry sends the resource kind in the ``Sentry-Hook-Resource``
header (``issue`` / ``event_alert`` / ``error`` / ``metric_alert`` / ...). This
connector turns ``issue`` and ``event_alert`` deliveries — the ones an agent
would triage — into TriggerEvents; everything else is intentionally skipped
(returns ``None``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import structlog

from backend.intake.schema import TriggerEvent

logger = structlog.get_logger(__name__)


class WebhookError(ValueError):
    """Raised when a webhook cannot be parsed (malformed / missing fields)."""


class WebhookSignatureError(WebhookError):
    """Raised when HMAC signature verification fails — treat as forged."""


# Sentry resource kinds (``Sentry-Hook-Resource``) this connector acts on. An
# ``issue`` is a grouped error an agent would triage; an ``event_alert`` is a
# fired issue/metric alert rule. Anything else (installation, comment, ...) is
# skipped (returns None).
SUPPORTED_RESOURCES = frozenset({"issue", "event_alert"})


def verify_signature(secret: str, raw_body: bytes, signature_header: str | None) -> None:
    """Raise :class:`WebhookSignatureError` unless the signature matches.

    Sentry signs the raw request body with HMAC-SHA256 keyed by the integration
    client-secret and sends the bare hex digest in ``Sentry-Hook-Signature``.
    """
    if not signature_header:
        raise WebhookSignatureError("missing Sentry-Hook-Signature header")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise WebhookSignatureError("signature mismatch")


def _lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _extract_issue(body: dict[str, Any], resource: str) -> dict[str, Any]:
    """Pull the issue object out of a Sentry payload for either resource shape.

    An ``issue`` delivery nests the issue under ``data.issue``; an
    ``event_alert`` nests the triggering event under ``data.event`` and carries
    the issue id on ``data.event.issue_id`` / ``data.event.groupID``.
    """
    data = body.get("data") or {}
    issue = data.get("issue")
    if isinstance(issue, dict) and issue:
        return issue
    if resource == "event_alert":
        event = data.get("event") or {}
        if isinstance(event, dict) and event:
            return event
    return {}


def _idempotency_key(body: dict[str, Any], issue: dict[str, Any], resource: str) -> str | None:
    """Derive a stable idempotency key from the Sentry issue / event id.

    Prefers the Sentry-supplied ``id`` (a redelivery of the same hook reuses
    it). Falls back through the issue id and the inner event id so a delivery
    without a top-level id still collapses on the resource it acts on.
    """
    for candidate in (
        body.get("id"),
        issue.get("id"),
        issue.get("issue_id"),
        issue.get("groupID"),
        issue.get("event_id"),
    ):
        if candidate:
            return f"sentry:{resource}:{candidate}"
    return None


def parse_webhook(
    *,
    workspace_id: uuid.UUID,
    headers: dict[str, str],
    raw_body: bytes,
    secret: str | None = None,
) -> TriggerEvent | None:
    """Parse one Sentry webhook delivery.

    Returns a :class:`TriggerEvent` for supported resources (``issue`` /
    ``event_alert``), or ``None`` to skip (unsupported resource). Raises
    :class:`WebhookSignatureError` when ``secret`` is given and verification
    fails, :class:`WebhookError` on malformed input.
    """
    h = _lower_headers(headers)

    if secret is not None:
        verify_signature(secret, raw_body, h.get("sentry-hook-signature"))

    resource = h.get("sentry-hook-resource")
    if resource not in SUPPORTED_RESOURCES:
        logger.debug("sentry_webhook_skip_resource", resource=resource)
        return None

    try:
        body: dict[str, Any] = json.loads(raw_body)
    except (ValueError, TypeError) as exc:
        raise WebhookError(f"sentry webhook body is not valid JSON: {exc}") from exc

    action = body.get("action")
    issue = _extract_issue(body, resource)

    idempotency_key = _idempotency_key(body, issue, resource)
    if not idempotency_key:
        raise WebhookError("sentry webhook missing a usable id (issue/event id)")

    payload = {
        "sentry_resource": resource,
        "action": action,
        "issue_id": issue.get("id") or issue.get("issue_id") or issue.get("groupID"),
        "title": issue.get("title"),
        "culprit": issue.get("culprit"),
        "level": issue.get("level"),
        "permalink": issue.get("permalink") or issue.get("web_url"),
        "project": (body.get("data") or {}).get("project")
        or (issue.get("project") if isinstance(issue.get("project"), str) else None),
        "body": body,
    }
    return TriggerEvent(
        workspace_id=workspace_id,
        source="sentry",
        trigger_kind="webhook",
        idempotency_key=idempotency_key,
        payload=payload,
        trace_id=idempotency_key,
    )


__all__ = [
    "SUPPORTED_RESOURCES",
    "WebhookError",
    "WebhookSignatureError",
    "parse_webhook",
    "verify_signature",
]
