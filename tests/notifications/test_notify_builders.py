"""NOTIFY_EVENT_BUILDERS — the seam that defines "notification channel" (N1a/N2).

N1a pinned the seam (which connectors are notify channels); N2 implements the
shaping bodies. These tests pin BOTH: exactly the four notify connectors are
registered with keys matching the connector ``name=`` (``email-sender``, not
``email``), the non-notify connectors are absent, and each builder shapes a
:class:`NotificationContent` + the founder-set ``delivery_config`` into the
exact send payload the connector's ``@p.outbound`` consumes — with routing
sourced from config (a missing target is a ``ValueError`` soft-fail, never a
send to a default target).
"""

from __future__ import annotations

import pytest

from backend.notifications.notify_builders import (
    NOTIFY_EVENT_BUILDERS,
    NotificationContent,
    ShapedNotification,
    build_email_notification,
)

_CONTENT = NotificationContent(
    event="needs_you",
    title="A run needs your decision",
    body="Postgres or SQLite?",
    link="https://app.example/decisions",
)


def test_notify_builders_are_exactly_the_four_notify_connectors() -> None:
    assert set(NOTIFY_EVENT_BUILDERS.keys()) == {
        "slack",
        "telegram",
        "discord",
        "email-sender",
    }


def test_email_connector_key_matches_plugin_name_not_bare_email() -> None:
    # The plugin name is ``email-sender``; a bare ``email`` key would never match
    # a ``connector_accounts.connector`` value, so the channel would be invisible.
    assert "email-sender" in NOTIFY_EVENT_BUILDERS
    assert "email" not in NOTIFY_EVENT_BUILDERS


@pytest.mark.parametrize("seam", ["notion", "linear", "trello", "github", "sentry"])
def test_non_notify_connectors_are_a_deliberate_seam(seam: str) -> None:
    assert seam not in NOTIFY_EVENT_BUILDERS


def test_slack_shapes_channel_text_and_bot_token_slot() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["slack"](_CONTENT, {"channel": "C123"})
    assert shaped.artifact_type == "slack_message"
    assert shaped.payload["channel"] == "C123"
    assert "Postgres or SQLite?" in shaped.payload["text"]
    assert "A run needs your decision" in shaped.payload["text"]
    assert "https://app.example/decisions" in shaped.payload["text"]
    assert shaped.credential_key == "bot_token"


def test_telegram_shapes_chat_id_text_and_bot_token_slot() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["telegram"](_CONTENT, {"chat_id": 42})
    assert shaped.artifact_type == "telegram_message"
    assert shaped.payload["chat_id"] == "42"
    assert "Postgres or SQLite?" in shaped.payload["text"]
    assert shaped.credential_key == "bot_token"


def test_discord_shapes_channel_id_content_and_bot_token_slot() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["discord"](_CONTENT, {"channel_id": "999"})
    assert shaped.artifact_type == "discord_message"
    assert shaped.payload["channel_id"] == "999"
    assert "Postgres or SQLite?" in shaped.payload["content"]
    assert shaped.credential_key == "bot_token"


def test_email_shapes_subject_body_and_api_key_slot() -> None:
    shaped = build_email_notification(_CONTENT, {"to": "founder@example.com"})
    assert shaped.artifact_type == "email"
    assert shaped.payload["to"] == "founder@example.com"
    assert shaped.payload["subject"] == "A run needs your decision"
    assert "Postgres or SQLite?" in shaped.payload["body"]
    assert "https://app.example/decisions" in shaped.payload["body"]
    assert shaped.payload["as_text"] is True
    assert shaped.credential_key == "api_key"


def test_email_optional_from_override_is_passed_through_only_when_set() -> None:
    without = build_email_notification(_CONTENT, {"to": "a@b.c"})
    assert "from" not in without.payload
    with_from = build_email_notification(_CONTENT, {"to": "a@b.c", "from": "bot@bsvibe.dev"})
    assert with_from.payload["from"] == "bot@bsvibe.dev"


@pytest.mark.parametrize(
    ("connector", "config"),
    [
        ("slack", {}),
        ("telegram", {}),
        ("discord", {}),
        ("email-sender", {}),
    ],
)
def test_missing_routing_target_is_a_valueerror_soft_fail(connector: str, config: dict) -> None:
    # A misconfigured channel must raise (the worker soft-fails it) rather than
    # send to a default target.
    with pytest.raises(ValueError):
        NOTIFY_EVENT_BUILDERS[connector](_CONTENT, config)


def test_shaped_notification_defaults() -> None:
    shaped = ShapedNotification(artifact_type="telegram_message", payload={"text": "hi"})
    assert shaped.credential_key == "token"
    assert shaped.extra_credentials == {}


def test_notification_content_carries_event_title_body_link() -> None:
    content = NotificationContent(
        event="needs_you", title="Decision", body="A run is waiting", link="/decisions"
    )
    assert content.event == "needs_you"
    assert content.title == "Decision"
    assert content.body == "A run is waiting"
    assert content.link == "/decisions"


def test_notification_content_deliverable_id_and_language_default() -> None:
    content = NotificationContent(event="shipped", title="t", body="b")
    assert content.deliverable_id is None
    assert content.language == "en"


def _shipped_content(*, deliverable_id: str | None, language: str) -> NotificationContent:
    return NotificationContent(
        event="shipped",
        title="작업 완료",
        body="검증까지 끝났어요.",
        link="/deliverables/x",
        deliverable_id=deliverable_id,
        language=language,
    )


def test_telegram_shipped_ko_carries_approve_reject_inline_keyboard() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["telegram"](
        _shipped_content(deliverable_id="DELIV-1", language="ko"), {"chat_id": 42}
    )
    markup = shaped.payload["reply_markup"]
    row = markup["inline_keyboard"][0]
    assert row[0]["text"] == "승인"
    assert row[0]["callback_data"] == "apv:DELIV-1"
    assert row[1]["text"] == "거절"
    assert row[1]["callback_data"] == "rej:DELIV-1"


def test_telegram_shipped_en_labels() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["telegram"](
        _shipped_content(deliverable_id="DELIV-2", language="en"), {"chat_id": 42}
    )
    row = shaped.payload["reply_markup"]["inline_keyboard"][0]
    assert row[0]["text"] == "Approve"
    assert row[1]["text"] == "Reject"


def test_telegram_shipped_without_deliverable_id_has_no_buttons() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["telegram"](
        _shipped_content(deliverable_id=None, language="ko"), {"chat_id": 42}
    )
    assert "reply_markup" not in shaped.payload


def test_telegram_non_shipped_event_has_no_buttons_even_with_deliverable_id() -> None:
    content = NotificationContent(
        event="needs_you",
        title="Decision",
        body="waiting",
        link="/decisions",
        deliverable_id="DELIV-3",
        language="ko",
    )
    shaped = NOTIFY_EVENT_BUILDERS["telegram"](content, {"chat_id": 42})
    assert "reply_markup" not in shaped.payload


def test_slack_shipped_with_deliverable_id_has_no_reply_markup() -> None:
    # Buttons are a telegram-only affordance; other channels stay unchanged.
    shaped = NOTIFY_EVENT_BUILDERS["slack"](
        _shipped_content(deliverable_id="DELIV-4", language="ko"), {"channel": "C1"}
    )
    assert "reply_markup" not in shaped.payload


def test_callback_data_fits_telegram_64_byte_cap() -> None:
    deliverable_id = "123e4567-e89b-12d3-a456-426614174000"
    shaped = NOTIFY_EVENT_BUILDERS["telegram"](
        _shipped_content(deliverable_id=deliverable_id, language="en"), {"chat_id": 42}
    )
    for btn in shaped.payload["reply_markup"]["inline_keyboard"][0]:
        assert len(btn["callback_data"].encode("utf-8")) <= 64
