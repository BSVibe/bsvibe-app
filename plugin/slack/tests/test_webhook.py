"""Tests for the slack plugin event parser — signing-secret HMAC verify,
timestamp replay protection, idempotency, and TriggerEvent mapping
(Workflow §3.1 / §6 #4 inbound capability)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest

from backend.intake.schema import TriggerEvent
from plugin.slack.webhook import (
    WebhookError,
    WebhookSignatureError,
    parse_event,
    verify_signature,
)

WORKSPACE = uuid.uuid4()
SECRET = "shhh-signing"


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def _headers(body: bytes, *, secret: str = SECRET, timestamp: str | None = None) -> dict:
    ts = timestamp if timestamp is not None else str(int(time.time()))
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": _sign(secret, ts, body),
    }


class TestVerifySignature:
    def test_accepts_valid_signature(self):
        body = b'{"a":1}'
        ts = str(int(time.time()))
        verify_signature(SECRET, body, _sign(SECRET, ts, body), ts)  # no raise

    def test_rejects_wrong_signature(self):
        body = b'{"a":1}'
        ts = str(int(time.time()))
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            verify_signature(SECRET, body, _sign("other", ts, body), ts)

    def test_rejects_missing_signature(self):
        ts = str(int(time.time()))
        with pytest.raises(WebhookSignatureError, match="missing"):
            verify_signature(SECRET, b"x", None, ts)

    def test_rejects_missing_timestamp(self):
        body = b"x"
        with pytest.raises(WebhookSignatureError, match="Timestamp"):
            verify_signature(SECRET, body, _sign(SECRET, "1", body), None)

    def test_rejects_stale_timestamp(self):
        body = b'{"a":1}'
        stale = str(int(time.time()) - 60 * 6)  # 6 minutes old → replay window
        with pytest.raises(WebhookSignatureError, match="stale"):
            verify_signature(SECRET, body, _sign(SECRET, stale, body), stale)

    def test_rejects_non_numeric_timestamp(self):
        body = b"x"
        with pytest.raises(WebhookSignatureError, match="Timestamp"):
            verify_signature(SECRET, body, _sign(SECRET, "abc", body), "abc")


class TestParseEvent:
    def test_url_verification_challenge_returns_none(self):
        body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
        evt = parse_event(
            workspace_id=WORKSPACE,
            headers=_headers(body),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is None

    def test_parses_app_mention_into_trigger_event(self):
        body = json.dumps(
            {
                "type": "event_callback",
                "event_id": "Ev123",
                "team_id": "T1",
                "event": {
                    "type": "app_mention",
                    "channel": "C9",
                    "user": "U1",
                    "text": "<@U0> hi",
                    "ts": "1700000000.000100",
                },
            }
        ).encode()
        evt = parse_event(
            workspace_id=WORKSPACE,
            headers=_headers(body),
            raw_body=body,
            secret=SECRET,
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.source == "slack"
        assert evt.trigger_kind == "webhook"
        assert evt.idempotency_key == "Ev123"
        assert evt.workspace_id == WORKSPACE
        assert evt.payload["slack_event"] == "app_mention"
        assert evt.payload["channel"] == "C9"

    def test_parses_message_event(self):
        body = json.dumps(
            {
                "type": "event_callback",
                "event_id": "Ev456",
                "event": {
                    "type": "message",
                    "channel": "C1",
                    "user": "U2",
                    "text": "hello",
                    "ts": "1700000000.000200",
                },
            }
        ).encode()
        evt = parse_event(
            workspace_id=WORKSPACE,
            headers=_headers(body),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is not None
        assert evt.payload["slack_event"] == "message"

    def test_unsupported_event_returns_none(self):
        body = json.dumps(
            {
                "type": "event_callback",
                "event_id": "Ev789",
                "event": {"type": "reaction_added", "channel": "C1"},
            }
        ).encode()
        evt = parse_event(
            workspace_id=WORKSPACE,
            headers=_headers(body),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is None

    def test_bot_message_skipped(self):
        body = json.dumps(
            {
                "type": "event_callback",
                "event_id": "EvBot",
                "event": {
                    "type": "message",
                    "channel": "C1",
                    "bot_id": "B1",
                    "text": "from bot",
                    "ts": "1.1",
                },
            }
        ).encode()
        evt = parse_event(
            workspace_id=WORKSPACE,
            headers=_headers(body),
            raw_body=body,
            secret=SECRET,
        )
        assert evt is None

    def test_bad_signature_raises(self):
        body = json.dumps(
            {"type": "event_callback", "event_id": "Ev1", "event": {"type": "app_mention"}}
        ).encode()
        ts = str(int(time.time()))
        headers = {
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": _sign("wrong-secret", ts, body),
        }
        with pytest.raises(WebhookSignatureError):
            parse_event(
                workspace_id=WORKSPACE,
                headers=headers,
                raw_body=body,
                secret=SECRET,
            )

    def test_stale_timestamp_raises(self):
        body = json.dumps(
            {"type": "event_callback", "event_id": "Ev1", "event": {"type": "app_mention"}}
        ).encode()
        stale = str(int(time.time()) - 60 * 10)
        headers = {
            "X-Slack-Request-Timestamp": stale,
            "X-Slack-Signature": _sign(SECRET, stale, body),
        }
        with pytest.raises(WebhookSignatureError, match="stale"):
            parse_event(
                workspace_id=WORKSPACE,
                headers=headers,
                raw_body=body,
                secret=SECRET,
            )

    def test_no_secret_skips_verification(self):
        body = json.dumps(
            {
                "type": "event_callback",
                "event_id": "EvNoSec",
                "event": {"type": "app_mention", "channel": "C1", "ts": "1.1"},
            }
        ).encode()
        evt = parse_event(
            workspace_id=WORKSPACE,
            headers={},  # no signature headers at all
            raw_body=body,
            secret=None,
        )
        assert evt is not None
        assert evt.idempotency_key == "EvNoSec"

    def test_missing_event_id_raises(self):
        body = json.dumps(
            {"type": "event_callback", "event": {"type": "app_mention", "channel": "C1"}}
        ).encode()
        with pytest.raises(WebhookError, match="event_id"):
            parse_event(
                workspace_id=WORKSPACE,
                headers={},
                raw_body=body,
                secret=None,
            )

    def test_malformed_json_raises(self):
        with pytest.raises(WebhookError, match="JSON"):
            parse_event(
                workspace_id=WORKSPACE,
                headers={},
                raw_body=b"{not json",
                secret=None,
            )
