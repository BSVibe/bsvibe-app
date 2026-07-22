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
    # reply_markup is a telegram-only key; slack uses Block Kit ``blocks`` instead.
    shaped = NOTIFY_EVENT_BUILDERS["slack"](
        _shipped_content(deliverable_id="DELIV-4", language="ko"), {"channel": "C1"}
    )
    assert "reply_markup" not in shaped.payload


def test_slack_shipped_with_deliverable_id_carries_block_kit_buttons() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["slack"](
        _shipped_content(deliverable_id="DELIV-9", language="ko"), {"channel": "C1"}
    )
    blocks = shaped.payload["blocks"]
    # A text fallback is kept alongside the blocks (accessibility / notifications).
    assert shaped.payload["text"]
    section = next(b for b in blocks if b["type"] == "section")
    assert section["text"]["type"] == "mrkdwn"
    actions = next(b for b in blocks if b["type"] == "actions")
    approve, reject = actions["elements"]
    assert approve["text"]["text"] == "승인"
    assert approve["value"] == "apv:DELIV-9"
    assert reject["text"]["text"] == "거절"
    assert reject["value"] == "rej:DELIV-9"


def test_slack_shipped_en_button_labels() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["slack"](
        _shipped_content(deliverable_id="DELIV-10", language="en"), {"channel": "C1"}
    )
    actions = next(b for b in shaped.payload["blocks"] if b["type"] == "actions")
    labels = [e["text"]["text"] for e in actions["elements"]]
    assert labels == ["Approve", "Reject"]


def test_slack_cta_is_mrkdwn_link_in_section_block() -> None:
    content = NotificationContent(
        event="shipped",
        title="작업 완료",
        body="검증까지 끝났어요.",
        link="보고서 보기 → https://app.bsvibe.dev/deliverables/D9",
        cta_label="보고서 보기",
        cta_url="https://app.bsvibe.dev/deliverables/D9",
        deliverable_id="DELIV-11",
        language="ko",
    )
    shaped = NOTIFY_EVENT_BUILDERS["slack"](content, {"channel": "C1"})
    section = next(b for b in shaped.payload["blocks"] if b["type"] == "section")
    assert "<https://app.bsvibe.dev/deliverables/D9|보고서 보기>" in section["text"]["text"]


def test_slack_non_shipped_event_has_no_blocks() -> None:
    content = NotificationContent(
        event="needs_you",
        title="Decision",
        body="waiting",
        link="/d",
        deliverable_id="DELIV-12",
        language="ko",
    )
    shaped = NOTIFY_EVENT_BUILDERS["slack"](content, {"channel": "C1"})
    assert "blocks" not in shaped.payload


def test_slack_shipped_without_deliverable_id_has_no_blocks() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["slack"](
        _shipped_content(deliverable_id=None, language="ko"), {"channel": "C1"}
    )
    assert "blocks" not in shaped.payload


def test_callback_data_fits_telegram_64_byte_cap() -> None:
    deliverable_id = "123e4567-e89b-12d3-a456-426614174000"
    shaped = NOTIFY_EVENT_BUILDERS["telegram"](
        _shipped_content(deliverable_id=deliverable_id, language="en"), {"chat_id": 42}
    )
    for btn in shaped.payload["reply_markup"]["inline_keyboard"][0]:
        assert len(btn["callback_data"].encode("utf-8")) <= 64


# ── FB2: telegram card is HTML with a tappable "보고서 보기" anchor CTA ────────────


def _shipped_with_cta(
    *, title: str, cta_label: str = "보고서 보기", url: str
) -> NotificationContent:
    return NotificationContent(
        event="shipped",
        title=title,
        body="검증까지 끝났어요.",
        # ``link`` still carries the flattened form (plain-text channels use it);
        # the telegram builder prefers the split cta parts to build the anchor.
        link=f"{cta_label} → {url}",
        cta_label=cta_label,
        cta_url=url,
        language="ko",
    )


def test_telegram_notification_uses_html_parse_mode() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["telegram"](
        _shipped_with_cta(title="작업 완료", url="https://app.bsvibe.dev/deliverables/D9"),
        {"chat_id": 42},
    )
    assert shaped.payload["parse_mode"] == "HTML"


def test_telegram_cta_is_a_tappable_anchor_not_a_raw_url() -> None:
    url = "https://app.bsvibe.dev/deliverables/D9"
    shaped = NOTIFY_EVENT_BUILDERS["telegram"](
        _shipped_with_cta(title="작업 완료", url=url), {"chat_id": 42}
    )
    text = shaped.payload["text"]
    # The WORDS are the link — an <a> anchor around the localized label.
    assert f'<a href="{url}">보고서 보기</a>' in text
    # The bare "label → url" trailing form is NOT shown (the founder taps words).
    assert "→ https://" not in text


def test_telegram_html_escapes_dynamic_title_so_it_cannot_break_the_parse() -> None:
    # A deliverable title with <, > or & must be escaped — only the anchor is
    # literal markup, so an angle-bracket in the title can't break Telegram's HTML.
    shaped = NOTIFY_EVENT_BUILDERS["telegram"](
        _shipped_with_cta(
            title="Fix <script> & <b>bold</b>", url="https://app.bsvibe.dev/deliverables/D9"
        ),
        {"chat_id": 42},
    )
    text = shaped.payload["text"]
    assert "&lt;script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in text
    # The raw unescaped title tag never rides through into the HTML body.
    assert "<script>" not in text


def test_telegram_without_cta_parts_falls_back_to_escaped_plain_link() -> None:
    # A link-carrying content with NO split cta parts (e.g. a non-shipped push
    # rendered by an older path) still HTML-escapes the flat link line.
    content = NotificationContent(
        event="needs_you",
        title="A run needs your decision",
        body="a & b < c",
        link="Answer it → https://app.bsvibe.dev/brief",
    )
    shaped = NOTIFY_EVENT_BUILDERS["telegram"](content, {"chat_id": 42})
    assert shaped.payload["parse_mode"] == "HTML"
    assert "a &amp; b &lt; c" in shaped.payload["text"]


def test_slack_notification_stays_plain_text_no_html_no_anchor() -> None:
    # Other channels are unchanged: no parse_mode, plain "label → url" CTA text.
    shaped = NOTIFY_EVENT_BUILDERS["slack"](
        _shipped_with_cta(title="작업 완료", url="https://app.bsvibe.dev/deliverables/D9"),
        {"channel": "C1"},
    )
    assert "parse_mode" not in shaped.payload
    assert "<a href" not in shaped.payload["text"]
    assert "보고서 보기 → https://app.bsvibe.dev/deliverables/D9" in shaped.payload["text"]


def test_email_notification_stays_plain_text_no_html() -> None:
    shaped = build_email_notification(
        _shipped_with_cta(title="작업 완료", url="https://app.bsvibe.dev/deliverables/D9"),
        {"to": "founder@example.com"},
    )
    assert "parse_mode" not in shaped.payload
    assert "<a href" not in shaped.payload["body"]


# ── discord: shipped card gains message-component 승인/거절 buttons + md link ────────


def test_discord_shipped_ko_carries_approve_reject_components() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["discord"](
        _shipped_content(deliverable_id="DELIV-9", language="ko"), {"channel_id": "C1"}
    )
    components = shaped.payload["components"]
    # One action row (type 1) with two buttons (type 2).
    assert len(components) == 1
    row = components[0]
    assert row["type"] == 1
    approve, reject = row["components"]
    assert approve["type"] == 2
    assert approve["style"] == 3  # success
    assert approve["label"] == "승인"
    assert approve["custom_id"] == "apv:DELIV-9"
    assert reject["style"] == 4  # danger
    assert reject["label"] == "거절"
    assert reject["custom_id"] == "rej:DELIV-9"


def test_discord_shipped_en_button_labels() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["discord"](
        _shipped_content(deliverable_id="DELIV-10", language="en"), {"channel_id": "C1"}
    )
    labels = [b["label"] for b in shaped.payload["components"][0]["components"]]
    assert labels == ["Approve", "Reject"]


def test_discord_custom_id_within_100_char_cap() -> None:
    deliverable_id = "123e4567-e89b-12d3-a456-426614174000"
    shaped = NOTIFY_EVENT_BUILDERS["discord"](
        _shipped_content(deliverable_id=deliverable_id, language="en"), {"channel_id": "C1"}
    )
    for btn in shaped.payload["components"][0]["components"]:
        assert len(btn["custom_id"]) <= 100


def test_discord_cta_is_markdown_link_in_content() -> None:
    content = NotificationContent(
        event="shipped",
        title="작업 완료",
        body="검증까지 끝났어요.",
        link="보고서 보기 → https://app.bsvibe.dev/deliverables/D9",
        cta_label="보고서 보기",
        cta_url="https://app.bsvibe.dev/deliverables/D9",
        deliverable_id="DELIV-11",
        language="ko",
    )
    shaped = NOTIFY_EVENT_BUILDERS["discord"](content, {"channel_id": "C1"})
    assert "[보고서 보기](https://app.bsvibe.dev/deliverables/D9)" in shaped.payload["content"]


def test_discord_non_shipped_event_has_no_components() -> None:
    content = NotificationContent(
        event="needs_you",
        title="Decision",
        body="waiting",
        link="/d",
        deliverable_id="DELIV-12",
        language="ko",
    )
    shaped = NOTIFY_EVENT_BUILDERS["discord"](content, {"channel_id": "C1"})
    assert "components" not in shaped.payload


def test_discord_shipped_without_deliverable_id_has_no_components() -> None:
    shaped = NOTIFY_EVENT_BUILDERS["discord"](
        _shipped_content(deliverable_id=None, language="ko"), {"channel_id": "C1"}
    )
    assert "components" not in shaped.payload
