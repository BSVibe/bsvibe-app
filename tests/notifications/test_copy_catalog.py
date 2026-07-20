"""Unit tests for the notification push-copy catalog (KO/EN localization).

The five notification producers render their push ``{title, body}`` through
:func:`~backend.notifications.copy.notification_copy`, keyed by the workspace's
``workspaces.language``. These tests pin the catalog contract:

* the FRAMING (title + any fixed body) is localized per language,
* the founder's OWN verbatim text (a Decision question, a deliverable title, a
  failure reason, a trigger source) rides through unchanged, and
* an unknown language falls back to English.
"""

from __future__ import annotations

from backend.notifications.copy import NotificationCopy, notification_copy


def test_needs_you_ko_localizes_title_keeps_question_verbatim() -> None:
    copy = notification_copy("needs_you", "ko", detail="배포 전략을 어떻게 할까요?")
    assert isinstance(copy, NotificationCopy)
    assert copy.title == "결정이 필요한 작업이 있어요"
    # The founder's actual question stays verbatim — only the title framing is KO.
    assert copy.body == "배포 전략을 어떻게 할까요?"


def test_needs_you_en() -> None:
    copy = notification_copy("needs_you", "en", detail="Which deploy strategy?")
    assert copy.title == "A run needs your decision"
    assert copy.body == "Which deploy strategy?"


def test_needs_you_empty_detail_falls_back_localized() -> None:
    assert (
        notification_copy("needs_you", "ko", detail="").body
        == "작업이 멈췄고 결정을 기다리고 있어요."
    )
    en = notification_copy("needs_you", "en", detail="").body
    assert en and en != "작업이 멈췄고 결정을 기다리고 있어요."


def test_unknown_language_falls_back_to_english() -> None:
    copy = notification_copy("needs_you", "fr", detail="x")
    assert copy.title == "A run needs your decision"


def test_missing_language_falls_back_to_english() -> None:
    assert notification_copy("needs_you", "", detail="x").title == "A run needs your decision"


def test_triggered_ko_keeps_source_verbatim() -> None:
    copy = notification_copy("triggered", "ko", source="sentry")
    assert copy.title == "새 작업이 들어왔어요"
    assert "sentry" in copy.body


def test_triggered_en() -> None:
    copy = notification_copy("triggered", "en", source="github")
    assert copy.title == "New work came in"
    assert copy.body == "A github trigger started new work."


def test_shipped_ko_and_en() -> None:
    ko = notification_copy("shipped", "ko", detail="dedup 유틸 추가")
    assert ko.title == "검증된 산출물이 배포됐어요"
    assert ko.body == "dedup 유틸 추가"
    en = notification_copy("shipped", "en", detail="Add dedup util")
    assert en.title == "A verified deliverable shipped"
    assert en.body == "Add dedup util"


def test_shipped_empty_detail_localized_fallback() -> None:
    assert notification_copy("shipped", "ko", detail="").body == "검증된 산출물이 준비됐어요."


def test_failed_keeps_reason_verbatim() -> None:
    ko = notification_copy("failed", "ko", detail="frame could not classify")
    assert ko.title == "작업이 실패했어요"
    assert ko.body == "frame could not classify"
    assert notification_copy("failed", "en", detail="").title == "A run failed"


def test_daily_brief_counts_both_languages() -> None:
    en = notification_copy("daily_brief", "en", shipped=2, failed=1, pending=3)
    assert en.title == "Your daily brief"
    assert en.body == "2 shipped · 1 failed · 3 decisions awaiting you"
    ko = notification_copy("daily_brief", "ko", shipped=2, failed=1, pending=3)
    assert ko.title == "오늘의 요약"
    assert ko.body == "배포 2 · 실패 1 · 대기 결정 3"
