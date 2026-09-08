"""``round_cap_reached`` gets a dedicated Brief question + push body, and the two
consumers must state the SAME (now jargon-free) sentence.

Founder ruling 2026-09-08: the dedicated line used to quote BSVibe's own internal
round counts and ask the founder to judge whether "the approach" needed to change —
words and concepts the founder never chose or saw. It is now a fixed, jargon-free
sentence that states no internal number and asks only what to do with the outcome.
This is the FALLBACK line only: ``_drive_loop.py`` prefers an ``ask_user_question``
Decision with a grounded deliverable choice whenever the run's history supports one
(see ``backend.workflow.domain.round_cap_outcome``); this line fires only when it
doesn't. Negative controls prove the OTHER reasons (and the generic
``verification_failed`` kind line) are untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.notifications.copy import needs_you_reason_body
from backend.workflow.application._checkpoint_shared import _question_text

_GENERIC_VERIFICATION_FAILED_EN = "BSVibe couldn't verify this work — review it before it ships?"
_GENERIC_VERIFICATION_FAILED_KO = "BSVibe가 이 작업을 검증하지 못했어요 — 출시 전에 검토할까요?"
_GENERIC_NEEDS_YOU_FALLBACK_EN = "A run has paused and needs your input."

_ROUND_CAP_QUESTION_EN = (
    "BSVibe kept trying but couldn't finish this the way you described — "
    "what would you like to happen with it?"
)
_ROUND_CAP_QUESTION_KO = (
    "BSVibe가 계속 해봤지만 요청하신 대로 마무리하지 못했어요 — 어떻게 하면 좋을까요?"
)

# The exact banned vocabulary this reason must never surface again (en/ko).
_BANNED_EN = ("budget", "round", "attempt", "approach", "verif", "ceiling", "reviewer")
_BANNED_KO = ("예산", "라운드", "시도", "접근", "검증", "천장", "검토자")


def _decision(**payload: object) -> SimpleNamespace:
    return SimpleNamespace(decision="verification_failed", payload=payload)


# ── the dedicated line, both consumers ─────────────────────────────────────


def test_round_cap_reached_question_is_dedicated_en() -> None:
    d = _decision(reason="round_cap_reached", round_budget_declared=12, round_budget_used=12)
    q = _question_text(d, "en")
    assert q != _GENERIC_VERIFICATION_FAILED_EN
    assert q == _ROUND_CAP_QUESTION_EN


def test_round_cap_reached_question_is_dedicated_ko() -> None:
    d = _decision(reason="round_cap_reached", round_budget_declared=12, round_budget_used=12)
    q = _question_text(d, "ko")
    assert q != _GENERIC_VERIFICATION_FAILED_KO
    assert q == _ROUND_CAP_QUESTION_KO


def test_round_cap_reached_body_is_dedicated_en() -> None:
    body = needs_you_reason_body(
        "round_cap_reached", "en", {"round_budget_declared": 8, "round_budget_used": 8}
    )
    assert body != _GENERIC_NEEDS_YOU_FALLBACK_EN
    assert body == _ROUND_CAP_QUESTION_EN


def test_round_cap_reached_body_is_dedicated_ko() -> None:
    body = needs_you_reason_body(
        "round_cap_reached", "ko", {"round_budget_declared": 8, "round_budget_used": 8}
    )
    assert body == _ROUND_CAP_QUESTION_KO


def test_round_cap_reached_question_is_the_same_even_without_numbers() -> None:
    """The line no longer depends on measured numbers at all — it states none — so a
    payload with no ``round_budget_declared``/``_used`` still gets the dedicated line,
    not the generic fallback (unlike before this ruling)."""
    d = _decision(reason="round_cap_reached")
    assert _question_text(d, "en") == _ROUND_CAP_QUESTION_EN
    assert needs_you_reason_body("round_cap_reached", "en") == _ROUND_CAP_QUESTION_EN


# ── ⭐ negative control: no banned vocabulary in either consumer ───────────


def test_round_cap_reached_question_has_no_banned_vocabulary() -> None:
    for lang, banned in (("en", _BANNED_EN), ("ko", _BANNED_KO)):
        text = _question_text(_decision(reason="round_cap_reached"), lang)
        for word in banned:
            assert word not in text, f"{word!r} leaked into the {lang} question: {text!r}"


def test_round_cap_reached_body_has_no_banned_vocabulary() -> None:
    for lang, banned in (("en", _BANNED_EN), ("ko", _BANNED_KO)):
        text = needs_you_reason_body("round_cap_reached", lang)
        for word in banned:
            assert word not in text, f"{word!r} leaked into the {lang} push body: {text!r}"


# ── NC1: verification_failed WITHOUT the round_cap_reached reason is untouched ─


def test_verification_failed_other_reason_is_the_generic_line() -> None:
    d = _decision(reason="contract_failed")
    assert _question_text(d, "en") == _GENERIC_VERIFICATION_FAILED_EN
    assert needs_you_reason_body("contract_failed", "en") == _GENERIC_NEEDS_YOU_FALLBACK_EN


# ── NC2: other NAMED reasons (their own dedicated lines) are untouched ───────


def test_ci_failed_reason_unaffected() -> None:
    d = _decision(reason="ci_failed")
    assert _question_text(d, "en") == (
        "The checks on BSVibe's pull request failed — the failing one says why."
    )
    body = needs_you_reason_body("ci_failed", "en")
    assert body == "The checks on BSVibe's pull request failed."


def test_github_binding_unavailable_reason_unaffected() -> None:
    d = _decision(reason="github_binding_unavailable")
    assert _question_text(d, "en") == (
        "BSVibe lost access to this product's repo, so its open pull request can't be "
        "merged — reconnect GitHub, or merge it yourself."
    )
    body = needs_you_reason_body("github_binding_unavailable", "en")
    assert body == "BSVibe lost access to the repo, so its open pull request can't be merged."


# ── NC3 (symmetry): the Brief question and the push body must state the SAME
# sentence for the same Decision — deleting either side's dynamic branch fails
# THIS test, not just its own module's. ──────────────────────────────────────


def test_brief_and_push_state_the_same_sentence() -> None:
    payload = {
        "reason": "round_cap_reached",
        "round_budget_declared": 9,
        "round_budget_used": 7,
    }
    question = _question_text(
        SimpleNamespace(decision="verification_failed", payload=payload), "en"
    )
    body = needs_you_reason_body("round_cap_reached", "en", payload)

    assert question == body
