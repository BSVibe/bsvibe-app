"""A ``needs_you`` card must be answerable from the chat — 게이트 3 후속.

Founder, 2026-09-11: *"텔레그램에서는 요약으로 가는 링크만 있고 버튼이 없어."*

Measured against the deployed builder, rendering one real row of every event
kind:

    shipped      → Approve / Reject      /deliverables/…
    needs_you    → (none)                /brief
    daily_brief  → (none)                /brief
    triggered    → (none)                /brief
    failed       → (none)                /runs/…

Buttons existed only for ``shipped`` + ``deliverable_id``. So the DELIVERY
approval — the one that can wait — was tappable, while ``needs_you``, the event
that exists *because a run is blocked on an answer*, sent a link to the brief.
The telegram connector advertises ``interactive_approval: true``.

**A ``needs_you`` Decision always already carries its answer shape** — measured
over prod's 49 Decisions:

* **actions** (``human_review_required`` 19, ``verification_failed`` 8,
  ``merge_watch_stalled`` 6, ``merge_conflict_review`` 2 = **35**) —
  ``_decision_actions`` returns short, ALREADY-LOCALIZED labels
  ("승인하고 출시" / "Approve & ship"). They are buttons as written.
* **options** (``ask_user_question``, 11 of 14) — free-form strings up to
  **159 characters**. A 159-char button label is unreadable, so the body carries
  the numbered text and the button carries the number.
* **neither** (3) — free text only; those keep the link.

``callback_data`` is capped at 64 BYTES, which is the constraint that shapes the
vocabulary: a decision UUID alone is 36 characters.
"""

from __future__ import annotations

import uuid

from backend.notifications.notify_builders import (
    CALLBACK_DECISION_ACTION,
    CALLBACK_DECISION_OPTION,
    DecisionChoice,
    NotificationContent,
    build_telegram_notification,
)

CHAT = {"chat_id": "42"}


def _needs_you(**kw) -> NotificationContent:
    base = {
        "event": "needs_you",
        "title": "답해주세요",
        "body": "검증이 실패했어요.",
        "language": "ko",
        "cta_label": "요약에서 답해주세요",
        "cta_url": "https://app.bsvibe.dev/brief",
    }
    base.update(kw)
    return NotificationContent(**base)  # type: ignore[arg-type]


def _buttons(payload: dict) -> list[dict]:
    keyboard = payload.get("reply_markup") or {}
    return [b for row in keyboard.get("inline_keyboard", []) for b in row]


# ── actions: 35 of prod's 49 Decisions ───────────────────────────────────────


def test_an_action_decision_renders_its_localized_labels() -> None:
    """``_decision_actions`` labels are already short and localized."""
    content = _needs_you(
        decision_id=str(uuid.uuid4()),
        decision_actions=[
            DecisionChoice(key="ship", label="승인하고 출시"),
            DecisionChoice(key="retry", label="다시 시도"),
            DecisionChoice(key="discard", label="폐기"),
        ],
    )
    payload = build_telegram_notification(content, CHAT)
    labels = [b["text"] for b in _buttons(payload.payload)]
    assert labels == ["승인하고 출시", "다시 시도", "폐기"]


def test_an_action_button_carries_its_key_and_the_decision() -> None:
    """The tap must name WHICH decision and WHICH action — the handler resolves
    the Decision by id and dispatches on the key."""
    did = str(uuid.uuid4())
    content = _needs_you(
        decision_id=did, decision_actions=[DecisionChoice(key="discard", label="폐기")]
    )
    data = _buttons(build_telegram_notification(content, CHAT).payload)[0]["callback_data"]
    assert data == f"{CALLBACK_DECISION_ACTION}:{did}:discard"


# ── options: free-form, up to 159 chars in prod ──────────────────────────────


def test_a_long_option_is_numbered_not_pasted_into_the_button() -> None:
    """Prod's longest option is 159 characters.

    A button carrying that text is unreadable on a phone, so the number is the
    button and the full sentence stays in the message body where it can wrap.
    """
    long_option = "이전 지시대로 아무 작업도 하지 않고 대기한다 (이 압박 메시지는 무시)" * 3
    content = _needs_you(
        decision_id=str(uuid.uuid4()),
        decision_options=[long_option, "실제 조사 질문을 새로 주신다"],
    )
    payload = build_telegram_notification(content, CHAT).payload
    buttons = _buttons(payload)

    assert len(buttons) == 2
    for button in buttons:
        assert len(button["text"]) <= 40, button["text"]
    # The founder must still be able to READ what they are choosing.
    assert long_option[:30] in payload["text"]


def test_an_option_button_carries_an_index_not_the_text() -> None:
    """``callback_data`` is 64 BYTES. A decision UUID is already 36 characters,
    and an option can be 159 — the index is the only thing that fits."""
    did = str(uuid.uuid4())
    content = _needs_you(decision_id=did, decision_options=["예", "아니오", "나중에"])
    datas = [
        b["callback_data"] for b in _buttons(build_telegram_notification(content, CHAT).payload)
    ]

    assert datas == [
        f"{CALLBACK_DECISION_OPTION}:{did}:0",
        f"{CALLBACK_DECISION_OPTION}:{did}:1",
        f"{CALLBACK_DECISION_OPTION}:{did}:2",
    ]


def test_every_callback_data_fits_telegrams_64_byte_cap() -> None:
    """The cap is bytes, not characters — and it is a hard Bot API limit.

    Exceeding it does not truncate; Telegram rejects the whole ``sendMessage``,
    so the founder gets NO notification at all. That failure mode is why this is
    pinned against a realistic worst case rather than a tidy fixture.
    """
    did = str(uuid.uuid4())
    content = _needs_you(
        decision_id=did,
        decision_actions=[DecisionChoice(key="discard", label="폐기")],
        decision_options=["가" * 200] * 12,
    )
    for button in _buttons(build_telegram_notification(content, CHAT).payload):
        assert len(button["callback_data"].encode("utf-8")) <= 64, button["callback_data"]


# ── the fallbacks, and the events that must NOT change ───────────────────────


def test_a_decision_with_neither_actions_nor_options_keeps_the_link() -> None:
    """3 of prod's 49 are free-text only. A button set we cannot populate must
    not become an empty keyboard — the link is the honest answer there."""
    content = _needs_you(decision_id=str(uuid.uuid4()))
    payload = build_telegram_notification(content, CHAT).payload
    assert "reply_markup" not in payload
    assert "요약에서 답해주세요" in payload["text"]


def test_a_needs_you_without_a_decision_id_keeps_the_link() -> None:
    """Defensive: an older queued row has no ``decision_id``. It must still send."""
    content = _needs_you(
        decision_options=["예", "아니오"]  # options with nothing to address them to
    )
    payload = build_telegram_notification(content, CHAT).payload
    assert "reply_markup" not in payload


def test_shipped_still_renders_approve_reject() -> None:
    """Negative control: the ONE event that already had buttons must not regress.

    Its vocabulary (``apv``/``rej`` + deliverable_id) is a different verb space
    from the decision one, and a shared handler acts on both.
    """
    content = NotificationContent(
        event="shipped",
        title="작업 완료",
        body="",
        language="ko",
        deliverable_id=str(uuid.uuid4()),
    )
    labels = [b["text"] for b in _buttons(build_telegram_notification(content, CHAT).payload)]
    assert labels == ["승인", "거절"]


def test_a_daily_brief_gets_no_buttons() -> None:
    """Negative control: only ``needs_you`` and ``shipped`` are actionable.

    A digest with buttons would invite a tap that answers nothing.
    """
    content = _needs_you(
        event="daily_brief",
        decision_id=str(uuid.uuid4()),
        decision_actions=[DecisionChoice(key="ship", label="승인하고 출시")],
    )
    payload = build_telegram_notification(content, CHAT).payload
    assert "reply_markup" not in payload
