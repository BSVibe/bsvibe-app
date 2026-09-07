"""``round_cap_reached`` gets a dedicated Brief question + push body, and the two
consumers must state the SAME measured facts (round_budget_declared / _used).

Founder ruling 2026-09-07: exhausting the round cap is an internal-recovery
exhaustion, not a first verification failure — so its line asks whether the
request is possible as scoped, and must say what was actually tried (the
declared/used round counts), never an unmeasured claim. Negative controls
prove the OTHER reasons (and the generic ``verification_failed`` kind line)
are untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.notifications.copy import needs_you_reason_body
from backend.workflow.application._checkpoint_shared import _question_text

_GENERIC_VERIFICATION_FAILED_EN = "BSVibe couldn't verify this work — review it before it ships?"
_GENERIC_VERIFICATION_FAILED_KO = "BSVibe가 이 작업을 검증하지 못했어요 — 출시 전에 검토할까요?"
_GENERIC_NEEDS_YOU_FALLBACK_EN = "A run has paused and needs your input."


def _decision(**payload: object) -> SimpleNamespace:
    return SimpleNamespace(decision="verification_failed", payload=payload)


# ── the dedicated line, both consumers ──────────────────────────────────────


def test_round_cap_reached_question_is_dedicated_en() -> None:
    d = _decision(reason="round_cap_reached", round_budget_declared=12, round_budget_used=12)
    q = _question_text(d, "en")
    assert q != _GENERIC_VERIFICATION_FAILED_EN
    assert "12" in q


def test_round_cap_reached_question_is_dedicated_ko() -> None:
    d = _decision(reason="round_cap_reached", round_budget_declared=12, round_budget_used=12)
    q = _question_text(d, "ko")
    assert q != _GENERIC_VERIFICATION_FAILED_KO
    assert "12" in q


def test_round_cap_reached_body_is_dedicated_en() -> None:
    body = needs_you_reason_body(
        "round_cap_reached", "en", {"round_budget_declared": 8, "round_budget_used": 8}
    )
    assert body != _GENERIC_NEEDS_YOU_FALLBACK_EN
    assert "8" in body


def test_round_cap_reached_body_is_dedicated_ko() -> None:
    body = needs_you_reason_body(
        "round_cap_reached", "ko", {"round_budget_declared": 8, "round_budget_used": 8}
    )
    assert "8" in body


def test_round_cap_reached_falls_back_to_generic_when_numbers_missing() -> None:
    """No measured numbers on the payload -> no unmeasured claim, generic line instead."""
    d = _decision(reason="round_cap_reached")
    assert _question_text(d, "en") == _GENERIC_VERIFICATION_FAILED_EN
    assert needs_you_reason_body("round_cap_reached", "en") == _GENERIC_NEEDS_YOU_FALLBACK_EN


# ── NC1: verification_failed WITHOUT the round_cap_reached reason is untouched ─


def test_verification_failed_other_reason_is_the_generic_line() -> None:
    d = _decision(reason="contract_failed")
    assert _question_text(d, "en") == _GENERIC_VERIFICATION_FAILED_EN
    assert needs_you_reason_body("contract_failed", "en") == _GENERIC_NEEDS_YOU_FALLBACK_EN


# ── NC2: other NAMED reasons (their own dedicated lines) are untouched ─────────


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
# round counts for the same Decision — deleting either side's dynamic branch
# fails THIS test, not just its own module's. ─────────────────────────────────


def test_brief_and_push_state_the_same_round_counts() -> None:
    declared, used = 9, 7
    payload = {
        "reason": "round_cap_reached",
        "round_budget_declared": declared,
        "round_budget_used": used,
    }
    question = _question_text(
        SimpleNamespace(decision="verification_failed", payload=payload), "en"
    )
    body = needs_you_reason_body("round_cap_reached", "en", payload)

    assert str(declared) in question
    assert str(used) in question
    assert str(declared) in body
    assert str(used) in body
