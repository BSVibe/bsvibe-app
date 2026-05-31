"""Tests for the github plugin webhook parser — signature verify, idempotency,
and TriggerEvent mapping (Workflow §3.1 / §6 #4 inbound capability)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest

from backend.workflow.domain.incoming import TriggerEvent
from plugin.github.webhook import (
    WebhookError,
    WebhookSignatureError,
    parse_webhook,
    verify_signature,
)

WORKSPACE = uuid.uuid4()
SECRET = "shhh"


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _headers(
    event: str, delivery: str = "d-1", *, secret: str | None = SECRET, body: bytes = b""
) -> dict:
    h = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    if secret is not None:
        h["X-Hub-Signature-256"] = _sign(secret, body)
    return h


class TestVerifySignature:
    def test_accepts_valid_signature(self):
        body = b'{"a":1}'
        verify_signature(SECRET, body, _sign(SECRET, body))  # no raise

    def test_rejects_wrong_signature(self):
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            verify_signature(SECRET, b'{"a":1}', _sign("other", b'{"a":1}'))

    def test_rejects_missing_signature(self):
        with pytest.raises(WebhookSignatureError, match="missing"):
            verify_signature(SECRET, b"x", None)


class TestParseWebhook:
    def test_parses_pull_request_opened_into_trigger_event(self):
        body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "bsvibe/bsvibe-site"},
                "pull_request": {"number": 42, "title": "Add thing"},
                "sender": {"login": "alice", "type": "User"},
            }
        ).encode()
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers("pull_request", "del-42", body=body),
            raw_body=body,
            secret=SECRET,
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.source == "github"
        assert evt.trigger_kind == "webhook"
        assert evt.idempotency_key == "del-42"
        assert evt.workspace_id == WORKSPACE
        assert evt.payload["github_event"] == "pull_request"
        assert evt.payload["action"] == "opened"
        assert evt.payload["repo"] == "bsvibe/bsvibe-site"

    def test_parses_issue_comment_created(self):
        body = json.dumps(
            {
                "action": "created",
                "repository": {"full_name": "o/r"},
                "issue": {"number": 7},
                "comment": {"id": 99, "body": "hi"},
                "sender": {"type": "User"},
            }
        ).encode()
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers("issue_comment", body=body),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is not None
        assert evt.payload["github_event"] == "issue_comment"

    def test_ping_event_returns_none(self):
        body = b'{"zen":"x"}'
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers("ping", body=body),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is None

    def test_unsupported_event_returns_none(self):
        body = b'{"action":"created"}'
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers("star", body=body),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is None

    def test_unhandled_action_returns_none(self):
        body = json.dumps({"action": "closed", "repository": {"full_name": "o/r"}}).encode()
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers("pull_request", body=body),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is None

    def test_bot_sender_skipped(self):
        body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "o/r"},
                "pull_request": {"number": 1},
                "sender": {"type": "Bot"},
            }
        ).encode()
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers=_headers("pull_request", body=body),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is None

    def test_bad_signature_raises(self):
        body = json.dumps({"action": "opened", "repository": {"full_name": "o/r"}}).encode()
        headers = _headers("pull_request", body=body)
        headers["X-Hub-Signature-256"] = _sign("wrong-secret", body)
        with pytest.raises(WebhookSignatureError):
            parse_webhook(
                workspace_id=WORKSPACE,
                headers=headers,
                raw_body=body,
                secret=SECRET,
            )

    def test_no_secret_skips_verification(self):
        body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "o/r"},
                "pull_request": {"number": 5},
                "sender": {"type": "User"},
            }
        ).encode()
        # No signature header, secret=None → still parses.
        evt = parse_webhook(
            workspace_id=WORKSPACE,
            headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d9"},
            raw_body=body,
            secret=None,
        )
        assert evt is not None
        assert evt.idempotency_key == "d9"

    def test_missing_delivery_header_raises(self):
        body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "o/r"},
                "pull_request": {"number": 1},
                "sender": {"type": "User"},
            }
        ).encode()
        with pytest.raises(WebhookError, match="X-GitHub-Delivery"):
            parse_webhook(
                workspace_id=WORKSPACE,
                headers={"X-GitHub-Event": "pull_request"},
                raw_body=body,
                secret=None,
            )

    def test_malformed_json_raises(self):
        with pytest.raises(WebhookError, match="JSON"):
            parse_webhook(
                workspace_id=WORKSPACE,
                headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d"},
                raw_body=b"{not json",
                secret=None,
            )
