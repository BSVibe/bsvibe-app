"""PR7 — the ``merge_conflict_review`` Decision kind.

The agent, re-dispatched to resolve a merge conflict, raises the founder Decision
via the SAME ask mechanism (``record_question`` / ``ask_user_question``). While
the run is resolving a conflict (``payload["merge_conflict_resolving"]``), that
ask is minted as ``merge_conflict_review`` — a founder-actionable Decision that
carries the calm merge-conflict copy (EN + KO) and the retry/discard one-click
actions (NO ship: an unmerged conflict has nothing verified to ship).
"""

from __future__ import annotations

from typing import Any

from backend.workflow.application._checkpoint_shared import (
    _EXECUTOR_DECISION_ACTIONS,
    _EXECUTOR_DECISION_QUESTIONS,
    ACTION_DISCARD,
    ACTION_RETRY,
    ACTION_SHIP,
    _decision_actions,
    _question_text,
)
from backend.workflow.application.mcp_work_effects import _ask_decision_kind


class _Run:
    def __init__(self, payload: dict[str, Any] | None) -> None:
        self.payload = payload


class _Decision:
    def __init__(self, kind: str, payload: dict[str, Any] | None) -> None:
        self.decision = kind
        self.payload = payload


# --- the ask kind is chosen from the run's conflict-resolution marker -------


def test_ask_kind_is_merge_conflict_review_while_resolving() -> None:
    run = _Run({"merge_conflict_resolving": True})
    assert _ask_decision_kind(run) == "merge_conflict_review"


def test_ask_kind_is_plain_ask_by_default() -> None:
    assert _ask_decision_kind(_Run(None)) == "ask_user_question"
    assert _ask_decision_kind(_Run({})) == "ask_user_question"
    assert _ask_decision_kind(_Run({"intent_text": "x"})) == "ask_user_question"


# --- the kind is registered in both presentation maps -----------------------


def test_kind_has_both_locale_questions() -> None:
    variants = _EXECUTOR_DECISION_QUESTIONS["merge_conflict_review"]
    assert variants["en"] and variants["ko"]
    assert variants["en"] != variants["ko"]
    # The calm line is surfaced when the Decision recorded no verbatim question.
    d = _Decision("merge_conflict_review", {"reason": "ambiguous"})
    assert _question_text(d, "en") == variants["en"]
    assert _question_text(d, "ko") == variants["ko"]
    # A recorded question still rides through verbatim.
    d2 = _Decision("merge_conflict_review", {"question": "X or Y?"})
    assert _question_text(d2, "ko") == "X or Y?"


def test_kind_offers_retry_and_discard_but_not_ship() -> None:
    actions = _EXECUTOR_DECISION_ACTIONS["merge_conflict_review"]
    keys = {a.key for a in actions}
    assert keys == {ACTION_RETRY, ACTION_DISCARD}
    assert ACTION_SHIP not in keys
    for a in actions:
        assert a.label_en and a.label_ko
    # And it resolves through the shared decision-actions accessor.
    assert _decision_actions(_Decision("merge_conflict_review", {})) is not None
