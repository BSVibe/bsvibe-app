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

from backend.notifications.copy import (
    NEEDS_YOU_LINK,
    NotificationCopy,
    needs_you_reason_body,
    notification_copy,
    notification_cta,
)


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
    # Compact-card status label — terse, not a sentence (founder feedback: the old
    # "검증된 산출물이 배포됐어요" was over-long; "작업 완료" 정도면 충분).
    ko = notification_copy("shipped", "ko", detail="dedup 유틸 추가")
    assert ko.title == "작업 완료"
    assert ko.body == "dedup 유틸 추가"
    en = notification_copy("shipped", "en", detail="Add dedup util")
    assert en.title == "Done"
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


# ── NC1: verify-gate needs_you must not leak English honesty-gate jargon ───────


def test_needs_you_reason_weak_evidence_is_friendly_ko() -> None:
    """A system-minted verify-gate decision (reason=weak_evidence_no_gate, no
    ``question``) maps to a warm KO sentence — never the raw English
    honesty-grade rationale."""
    # Terse card-style verify line — no long apology (founder feedback).
    body = needs_you_reason_body("weak_evidence_no_gate", "ko")
    assert body == "작업을 마쳤지만 검증 근거가 약해요. 확인해주세요."
    # No leaked English jargon.
    for jargon in ("grade", "gate", "weak evidence", "verified"):
        assert jargon not in body


def test_needs_you_reason_weak_evidence_en() -> None:
    body = needs_you_reason_body("weak_evidence_no_gate", "en")
    assert body == "The work is done, but the evidence is weak — please review."


def test_needs_you_unknown_reason_falls_back_generic_localized() -> None:
    """Any OTHER system reason gets the generic localized fallback — the English
    ``decision.rationale`` is NEVER leaked through."""
    ko = needs_you_reason_body("no_executor_dispatch_transport", "ko")
    assert ko == "작업이 멈췄고 결정을 기다리고 있어요."
    en = needs_you_reason_body("", "en")
    assert en == "A run has paused and needs your input."


# ── NC3: absolute clickable link + localized CTA ──────────────────────────────


def test_needs_you_link_targets_the_brief_not_decisions() -> None:
    # The decisions tab was removed (unified into the Brief) — needs_you links there.
    assert NEEDS_YOU_LINK == "/brief"


def test_cta_needs_you_ko_is_absolute_and_localized() -> None:
    cta = notification_cta("needs_you", "ko", "https://app.example", "/brief")
    assert cta == "요약에서 답해주세요 → https://app.example/brief"


def test_cta_needs_you_en() -> None:
    cta = notification_cta("needs_you", "en", "https://app.example", "/brief")
    assert cta == "Answer it in your Brief → https://app.example/brief"


def test_cta_shipped_ko_uses_report_phrasing_and_absolute_url() -> None:
    # shipped links to its deliverable report — "보고서 보기 → <url>", not "요약에서…".
    cta = notification_cta("shipped", "ko", "https://app.example", "/deliverables/abc")
    assert cta == "보고서 보기 → https://app.example/deliverables/abc"


def test_cta_shipped_en_uses_report_phrasing() -> None:
    cta = notification_cta("shipped", "en", "https://app.example", "/deliverables/abc")
    assert cta == "View report → https://app.example/deliverables/abc"


def test_cta_review_en_for_non_decision_events() -> None:
    for event in ("triggered", "failed", "daily_brief"):
        cta = notification_cta(event, "en", "https://app.example", "/brief")
        assert cta == "Review it in your Brief → https://app.example/brief"


def test_cta_strips_trailing_slash_on_base_url() -> None:
    cta = notification_cta("triggered", "en", "https://app.example/", "/brief")
    assert cta == "Review it in your Brief → https://app.example/brief"
