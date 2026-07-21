"""Tests for the telegram plugin update parser — secret-token verify,
idempotency from update_id, non-message skip, and TriggerEvent mapping
(Workflow §3.1 / §6 #4 inbound capability)."""

from __future__ import annotations

import json
import uuid

import pytest

from backend.workflow.domain.incoming import TriggerEvent
from plugin.telegram.webhook import (
    SECRET_TOKEN_HEADER,
    WebhookError,
    WebhookSignatureError,
    parse_callback_query,
    parse_update,
    verify_secret_token,
)


def _callback_body(
    *,
    data: str = "apv:DELIV-1",
    from_id: int = 5,
    chat_type: str = "private",
) -> dict:
    return {
        "update_id": 500,
        "callback_query": {
            "id": "cbq-1",
            "from": {"id": from_id, "is_bot": False},
            "message": {
                "message_id": 77,
                "chat": {"id": 99, "type": chat_type},
            },
            "data": data,
        },
    }


class TestParseCallbackQuery:
    def test_extracts_all_fields_and_verb(self) -> None:
        parsed = parse_callback_query(_callback_body())
        assert parsed is not None
        assert parsed["callback_query_id"] == "cbq-1"
        assert parsed["from_id"] == 5
        assert parsed["chat_type"] == "private"
        assert parsed["chat_id"] == 99
        assert parsed["message_id"] == 77
        assert parsed["verb"] == "apv"
        assert parsed["deliverable_id"] == "DELIV-1"
        assert parsed["malformed"] is False

    def test_reject_verb(self) -> None:
        parsed = parse_callback_query(_callback_body(data="rej:DELIV-9"))
        assert parsed is not None
        assert parsed["verb"] == "rej"
        assert parsed["deliverable_id"] == "DELIV-9"

    def test_unknown_verb_is_malformed(self) -> None:
        parsed = parse_callback_query(_callback_body(data="del:DELIV-1"))
        assert parsed is not None
        assert parsed["malformed"] is True
        assert parsed["verb"] is None
        assert parsed["deliverable_id"] is None

    def test_missing_data_is_malformed(self) -> None:
        body = _callback_body()
        del body["callback_query"]["data"]
        parsed = parse_callback_query(body)
        assert parsed is not None
        assert parsed["malformed"] is True

    def test_no_callback_query_returns_none(self) -> None:
        assert parse_callback_query({"update_id": 1, "message": {}}) is None


WORKSPACE = uuid.uuid4()
SECRET = "shhh-secret-token"


def _headers(*, secret: str = SECRET) -> dict:
    return {"X-Telegram-Bot-Api-Secret-Token": secret}


def _message_update(update_id: int = 100) -> bytes:
    return json.dumps(
        {
            "update_id": update_id,
            "message": {
                "message_id": 11,
                "from": {"id": 5, "is_bot": False, "first_name": "Ada"},
                "chat": {"id": 99, "type": "private"},
                "text": "hello bot",
            },
        }
    ).encode()


class TestVerifySecretToken:
    def test_accepts_matching_token(self):
        verify_secret_token(SECRET, SECRET)  # no raise

    def test_rejects_wrong_token(self):
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            verify_secret_token(SECRET, "wrong")

    def test_rejects_missing_token(self):
        with pytest.raises(WebhookSignatureError, match="missing"):
            verify_secret_token(SECRET, None)

    def test_header_name_is_lowercased_canonical(self):
        assert SECRET_TOKEN_HEADER == "x-telegram-bot-api-secret-token"


class TestParseUpdate:
    def test_parses_message_into_trigger_event(self):
        body = _message_update(update_id=100)
        evt = parse_update(
            workspace_id=WORKSPACE,
            headers=_headers(),
            raw_body=body,
            secret=SECRET,
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.source == "telegram"
        assert evt.trigger_kind == "webhook"
        assert evt.idempotency_key == "telegram:100"
        assert evt.workspace_id == WORKSPACE
        assert evt.payload["telegram_update"] == "message"
        assert evt.payload["chat_id"] == 99
        assert evt.payload["text"] == "hello bot"

    def test_idempotency_key_from_update_id(self):
        # Two redeliveries of the same update_id collapse to the same key.
        first = parse_update(
            workspace_id=WORKSPACE,
            headers=_headers(),
            raw_body=_message_update(update_id=777),
            secret=SECRET,
        )
        second = parse_update(
            workspace_id=WORKSPACE,
            headers=_headers(),
            raw_body=_message_update(update_id=777),
            secret=SECRET,
        )
        assert first is not None and second is not None
        assert first.idempotency_key == second.idempotency_key == "telegram:777"

    def test_bad_secret_token_raises(self):
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            parse_update(
                workspace_id=WORKSPACE,
                headers=_headers(secret="not-the-secret"),
                raw_body=_message_update(),
                secret=SECRET,
            )

    def test_missing_secret_token_header_raises(self):
        with pytest.raises(WebhookSignatureError, match="missing"):
            parse_update(
                workspace_id=WORKSPACE,
                headers={},  # no secret-token header at all
                raw_body=_message_update(),
                secret=SECRET,
            )

    def test_no_secret_skips_verification(self):
        evt = parse_update(
            workspace_id=WORKSPACE,
            headers={},  # no secret-token header
            raw_body=_message_update(update_id=200),
            secret=None,
        )
        assert evt is not None
        assert evt.idempotency_key == "telegram:200"

    def test_non_message_update_returns_none(self):
        body = json.dumps(
            {
                "update_id": 300,
                "callback_query": {"id": "cb1", "data": "x"},
            }
        ).encode()
        evt = parse_update(workspace_id=WORKSPACE, headers=_headers(), raw_body=body, secret=SECRET)
        assert evt is None

    def test_edited_message_update_returns_none(self):
        body = json.dumps(
            {
                "update_id": 301,
                "edited_message": {"message_id": 1, "chat": {"id": 1}, "text": "x"},
            }
        ).encode()
        evt = parse_update(workspace_id=WORKSPACE, headers=_headers(), raw_body=body, secret=SECRET)
        assert evt is None

    def test_bot_authored_message_skipped(self):
        body = json.dumps(
            {
                "update_id": 400,
                "message": {
                    "message_id": 2,
                    "from": {"id": 9, "is_bot": True},
                    "chat": {"id": 99},
                    "text": "from bot",
                },
            }
        ).encode()
        evt = parse_update(workspace_id=WORKSPACE, headers=_headers(), raw_body=body, secret=SECRET)
        assert evt is None

    def test_missing_update_id_raises(self):
        body = json.dumps({"message": {"message_id": 1, "chat": {"id": 1}}}).encode()
        with pytest.raises(WebhookError, match="update_id"):
            parse_update(workspace_id=WORKSPACE, headers={}, raw_body=body, secret=None)

    def test_malformed_json_raises(self):
        with pytest.raises(WebhookError, match="JSON"):
            parse_update(
                workspace_id=WORKSPACE,
                headers={},
                raw_body=b"{not json",
                secret=None,
            )
