"""Tests for the sentry plugin webhook parser — client-secret HMAC-SHA256
verify, resource routing, idempotency, and TriggerEvent mapping
(Workflow §3.1 / §6 #4 inbound capability)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest

from backend.intake.schema import TriggerEvent
from backend.plugins.implementations.sentry.webhook import (
    SUPPORTED_RESOURCES,
    WebhookError,
    WebhookSignatureError,
    parse_webhook,
    verify_signature,
)

WORKSPACE = uuid.uuid4()
SECRET = "shhh-client-secret"


def _sign(secret: str, body: bytes) -> str:
    """Compute the bare-hex HMAC-SHA256 signature Sentry sends."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _headers(body: bytes, *, secret: str = SECRET, resource: str = "issue") -> dict:
    return {
        "Sentry-Hook-Signature": _sign(secret, body),
        "Sentry-Hook-Resource": resource,
    }


def _issue_body(issue_id: str = "100001", *, hook_id: str | None = "WH-1") -> bytes:
    body: dict = {
        "action": "created",
        "data": {
            "issue": {
                "id": issue_id,
                "title": "TypeError: undefined is not a function",
                "culprit": "app/main.py in handler",
                "level": "error",
                "permalink": f"https://sentry.io/org/proj/issues/{issue_id}/",
                "project": "proj",
            }
        },
    }
    if hook_id is not None:
        body["id"] = hook_id
    return json.dumps(body).encode()


class TestVerifySignature:
    def test_accepts_valid_signature(self):
        body = b'{"a":1}'
        verify_signature(SECRET, body, _sign(SECRET, body))  # no raise

    def test_rejects_wrong_signature(self):
        body = b'{"a":1}'
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            verify_signature(SECRET, body, _sign("other", body))

    def test_rejects_missing_signature(self):
        with pytest.raises(WebhookSignatureError, match="missing"):
            verify_signature(SECRET, b"x", None)

    def test_signature_is_constant_time_hex(self):
        # Sentry sends a bare hex digest (no "sha256=" prefix).
        body = b'{"x":2}'
        sig = _sign(SECRET, body)
        assert "=" not in sig
        verify_signature(SECRET, body, sig)


class TestParseWebhook:
    def test_parses_issue_webhook_into_trigger_event(self):
        body = _issue_body("100001")
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers(body, resource="issue"),
            raw_body=body,
            secret=SECRET,
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.source == "sentry"
        assert evt.trigger_kind == "webhook"
        assert evt.workspace_id == WORKSPACE
        assert evt.payload["sentry_resource"] == "issue"
        assert evt.payload["issue_id"] == "100001"
        assert evt.payload["title"].startswith("TypeError")
        assert evt.payload["level"] == "error"

    def test_idempotency_key_uses_hook_id_and_resource(self):
        body = _issue_body("100001", hook_id="WH-42")
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers(body, resource="issue"),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is not None
        assert evt.idempotency_key == "sentry:issue:WH-42"

    def test_idempotency_key_stable(self):
        body = _issue_body("777", hook_id="WH-stable")
        first = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers(body),
            raw_body=body,
            secret=SECRET,
        )
        second = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers(body),
            raw_body=body,
            secret=SECRET,
        )
        assert first is not None and second is not None
        assert first.idempotency_key == second.idempotency_key

    def test_idempotency_key_falls_back_to_issue_id(self):
        # No top-level hook id → derive from the issue id.
        body = _issue_body("555", hook_id=None)
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers(body, resource="issue"),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is not None
        assert evt.idempotency_key == "sentry:issue:555"

    def test_parses_event_alert_resource(self):
        body = json.dumps(
            {
                "id": "WH-alert",
                "action": "triggered",
                "data": {
                    "event": {
                        "issue_id": "200002",
                        "title": "RuntimeError",
                        "level": "fatal",
                        "web_url": "https://sentry.io/org/proj/issues/200002/",
                    }
                },
            }
        ).encode()
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers(body, resource="event_alert"),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is not None
        assert evt.payload["sentry_resource"] == "event_alert"
        assert evt.payload["issue_id"] == "200002"
        assert evt.idempotency_key == "sentry:event_alert:WH-alert"

    def test_unsupported_resource_returns_none(self):
        body = _issue_body("100001")
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers(body, resource="installation"),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is None

    def test_missing_resource_header_returns_none(self):
        body = _issue_body("100001")
        headers = {"Sentry-Hook-Signature": _sign(SECRET, body)}  # no resource header
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers=headers,
            raw_body=body,
            secret=SECRET,
        )
        assert evt is None

    def test_supported_resources_constant(self):
        assert "issue" in SUPPORTED_RESOURCES
        assert "event_alert" in SUPPORTED_RESOURCES
        assert "installation" not in SUPPORTED_RESOURCES

    def test_bad_signature_raises(self):
        body = _issue_body("100001")
        headers = {
            "Sentry-Hook-Signature": _sign("wrong-secret", body),
            "Sentry-Hook-Resource": "issue",
        }
        with pytest.raises(WebhookSignatureError):
            parse_webhook(
                workspace_id=WORKSPACE,
                headers=headers,
                raw_body=body,
                secret=SECRET,
            )

    def test_signature_checked_before_resource_routing(self):
        # A forged unsupported-resource delivery still fails closed.
        body = _issue_body("100001")
        headers = {
            "Sentry-Hook-Signature": _sign("wrong-secret", body),
            "Sentry-Hook-Resource": "installation",
        }
        with pytest.raises(WebhookSignatureError):
            parse_webhook(
                workspace_id=WORKSPACE,
                headers=headers,
                raw_body=body,
                secret=SECRET,
            )

    def test_no_secret_skips_verification(self):
        body = _issue_body("100001", hook_id="WH-nosec")
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers={"Sentry-Hook-Resource": "issue"},  # no signature header at all
            raw_body=body,
            secret=None,
        )
        assert evt is not None
        assert evt.idempotency_key == "sentry:issue:WH-nosec"

    def test_missing_id_raises(self):
        body = json.dumps(
            {"action": "created", "data": {"issue": {"title": "no id at all"}}}
        ).encode()
        with pytest.raises(WebhookError, match="id"):
            parse_webhook(
                workspace_id=WORKSPACE,
                headers={"Sentry-Hook-Resource": "issue"},
                raw_body=body,
                secret=None,
            )

    def test_malformed_json_raises(self):
        with pytest.raises(WebhookError, match="JSON"):
            parse_webhook(
                workspace_id=WORKSPACE,
                headers={"Sentry-Hook-Resource": "issue"},
                raw_body=b"{not json",
                secret=None,
            )
