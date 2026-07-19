"""Shared paused-run-Decision (checkpoint) presentation helpers.

The kind → question / options / one-click-action derivation used by BOTH the
REST checkpoint list endpoints (:mod:`backend.api.v1.checkpoints`) and the
resolve service (:mod:`backend.workflow.application.checkpoint_resolution`) —
and, from C2 onward, the MCP checkpoint tools. It lives in the Workflow
application layer (not under ``backend.api``) so the MCP leaf surface can reuse
it without crossing the ``backend.mcp`` → ``backend.api`` import boundary.

Pure presentation: no DB, no side effects. Depends only on the
:class:`~backend.workflow.infrastructure.db.Decision` shape + pydantic.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from backend.workflow.infrastructure.db import Decision


class DecisionAction(BaseModel):
    """L-D2: a one-click action available on an executor B2b Decision.

    The founder clicks the action (PWA renders a dedicated button) instead of
    typing a free-text resolution; the resolve endpoint dispatches on ``key``
    to a side-effecting handler (e.g. ``ship`` promotes the run to shipped +
    creates the deliverable; ``discard`` abandons it). Labels are sent for
    every supported locale so the PWA renders them client-side without a
    per-product i18n lookup."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label_en: str
    label_ko: str


# B4: executor B2b Decisions (raised when an executor run does NOT verify) record
# ``payload.reason`` instead of ``payload.question`` — they are an honest "this
# needs you" surfaced as a Decision, not a work-LLM question. Map the kind →
# a calm, human-readable line so the founder never sees a blank question on a
# genuinely actionable needs-you item.
# Per-language so a ko workspace's founder reads the needs-you line in Korean.
# These are FIXED system strings (no work-LLM question), so they can't ride the
# generation adapter's language_directive — they're localized here by the
# workspace output language, mirroring DecisionAction's label_en / label_ko.
_EXECUTOR_DECISION_QUESTIONS: dict[str, dict[str, str]] = {
    "verification_failed": {
        "en": "BSVibe couldn't verify this work — review it before it ships?",
        "ko": "BSVibe가 이 작업을 검증하지 못했어요 — 출시 전에 검토할까요?",
    },
    "human_review_required": {
        "en": "This work needs your review before BSVibe can call it verified.",
        "ko": "이 작업은 검증됨으로 표시하기 전에 검토가 필요해요.",
    },
}


# L-D2: per-kind action specs surfaced on every executor B2b Decision the
# founder can act on with one click. Labels ship for every supported locale
# so the PWA can render them without an extra round-trip. Action ``key``s
# are stable wire identifiers — handlers dispatch on them in
# :func:`~backend.workflow.application.checkpoint_resolution.resolve_checkpoint`.
# Adding a new action = one entry here + one handler. New Decision kinds may
# opt in by adding themselves to this map.
ACTION_SHIP = "ship"
ACTION_DISCARD = "discard"
# L2 (#9): re-open the paused run for another attempt instead of shipping a
# possibly-broken result or abandoning it. ``retry`` carries NO dedicated
# handler — it falls through to the resume branch in ``resolve_checkpoint``
# (RUNNING → OPEN), so ``AgentWorker.drive_once`` re-picks the run and drives a
# fresh attempt. A failed run is recoverable, not a dead-end.
ACTION_RETRY = "retry"

_EXECUTOR_DECISION_ACTIONS: dict[str, list[DecisionAction]] = {
    "verification_failed": [
        DecisionAction(key=ACTION_SHIP, label_en="Approve & ship", label_ko="승인하고 출시"),
        DecisionAction(key=ACTION_RETRY, label_en="Retry", label_ko="다시 시도"),
        DecisionAction(key=ACTION_DISCARD, label_en="Discard", label_ko="폐기"),
    ],
    "human_review_required": [
        DecisionAction(key=ACTION_SHIP, label_en="Approve & ship", label_ko="승인하고 출시"),
        DecisionAction(key=ACTION_RETRY, label_en="Retry", label_ko="다시 시도"),
        DecisionAction(key=ACTION_DISCARD, label_en="Discard", label_ko="폐기"),
    ],
    # W1: the ship_or_discard kind from L-P2 is retired. Verified runs no
    # longer need a founder-approval gate; W2 wires the actual auto-merge.
}


def _decision_actions(decision: Decision) -> list[DecisionAction] | None:
    """The structured one-click actions for ``decision``, or ``None`` if the
    kind doesn't carry any (a vanilla ask_user_question Decision)."""
    return _EXECUTOR_DECISION_ACTIONS.get(decision.decision)


def _question_text(decision: Decision, language: str = "en") -> str:
    """The founder-facing question for a paused-run Decision, in ``language``.

    Prefers the work LLM's recorded ``payload.question`` (the ``ask_user_question``
    path) — already in the founder's language (generated via the localized
    adapter), so ``language`` never overrides it. For an executor B2b Decision —
    which records ``payload.reason``, not a question — fall back to a calm
    kind-derived line in ``language`` so the needs-you item is never blank. A
    wholly unrecognised reason-only Decision degrades to an empty string."""
    payload = decision.payload or {}
    if isinstance(payload, dict):
        value = payload.get("question")
        if isinstance(value, str) and value.strip():
            return value
    variants = _EXECUTOR_DECISION_QUESTIONS.get(decision.decision)
    if variants is None:
        return ""
    return variants.get(language) or variants.get("en") or ""


def _decision_options(decision: Decision) -> list[str] | None:
    """The structured options offered for this paused-run Decision, if any.

    B11a: the work LLM's ``ask_user_question`` may carry an ``options`` array on
    the Decision payload. Only return a clean list of non-empty strings; any
    other shape degrades to ``None`` so the PWA falls back to free-text and the
    resolve endpoint skips the membership check (existing behaviour)."""
    payload = decision.payload or {}
    if not isinstance(payload, dict):
        return None
    raw = payload.get("options")
    if not isinstance(raw, list):
        return None
    cleaned = [item for item in raw if isinstance(item, str) and item.strip()]
    return cleaned or None


__all__ = [
    "ACTION_DISCARD",
    "ACTION_RETRY",
    "ACTION_SHIP",
    "DecisionAction",
    "_decision_actions",
    "_decision_options",
    "_question_text",
]
