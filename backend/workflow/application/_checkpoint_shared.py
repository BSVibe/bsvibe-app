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
    # PR7 — the agent hit a merge conflict it judged AMBIGUOUS (two changes
    # touched the SAME logic) and raised it rather than guessing. Calm one-liner
    # for the checkpoint list when the agent recorded no verbatim question.
    "merge_conflict_review": {
        "en": "Two changes touched the same logic and BSVibe can't safely merge them — how should it resolve?",
        "ko": "두 변경이 같은 로직을 건드려 BSVibe가 안전하게 병합할 수 없어요 — 어떻게 처리할까요?",
    },
    # The merge watch gave up on an OPEN pull request. The generic line; the
    # per-reason lines below say WHICH way it gave up.
    "merge_watch_stalled": {
        "en": "BSVibe opened a pull request but couldn't finish merging it — it needs you.",
        "ko": "BSVibe가 올린 PR을 끝까지 병합하지 못했어요 — 확인이 필요해요.",
    },
    # The drive itself kept crashing (a timed-out turn, a worker that died).
    # Nothing was produced, so this asks whether to try again or let it go.
    "run_drive_failed": {
        "en": "This task kept failing to start, so BSVibe stopped retrying — try again, or let it go?",
        "ko": "이 작업이 계속 시작되지 못해서 재시도를 멈췄어요 — 다시 해볼까요, 접을까요?",
    },
}


# A stalled merge watch has more than one way to give up, and they call for
# different things from the founder (reconnect the repo vs. look at CI). So the
# needs-you line is keyed by the Decision ``payload["reason"]`` FIRST, exactly as
# the notification body is (:data:`~backend.notifications.copy._NEEDS_YOU_REASON_BODY`)
# — the phone and the Brief must say the same thing. An unlisted reason falls
# back to the kind line above, so a new reason is never a blank item.
_EXECUTOR_DECISION_REASON_QUESTIONS: dict[str, dict[str, str]] = {
    "github_binding_unavailable": {
        "en": "BSVibe lost access to this product's repo, so its open pull request can't be merged — reconnect GitHub, or merge it yourself.",
        "ko": "이 제품의 저장소 접근이 끊겨서 올려둔 PR을 병합할 수 없어요 — GitHub를 다시 연결하거나 직접 병합해주세요.",
    },
    "ci_deadline_exceeded": {
        "en": "The checks on BSVibe's pull request never went green in time — take a look at it.",
        "ko": "BSVibe가 올린 PR의 검사가 제한 시간 안에 통과하지 못했어요 — 한번 봐주세요.",
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
# A pure "I've seen this" — records the founder's acknowledgment and touches the
# run not at all. It exists for the INFORMATIONAL Decision: one raised about a
# run that already finished, where ship (nothing left to ship), retry (nothing
# left to drive) and discard (the work DID ship — cancelling it would be a lie)
# are all wrong answers. Handled in ``resolve_checkpoint`` by doing nothing.
ACTION_ACKNOWLEDGE = "acknowledge"

_EXECUTOR_DECISION_ACTIONS: dict[str, list[DecisionAction]] = {
    # A crashed drive produced nothing, so ``ship`` has no meaning here — the
    # only honest answers are another attempt or letting the run go.
    "run_drive_failed": [
        DecisionAction(key=ACTION_RETRY, label_en="Try again", label_ko="다시 시도"),
        DecisionAction(key=ACTION_DISCARD, label_en="Discard", label_ko="폐기"),
    ],
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
    # PR7 — the AMBIGUOUS-merge-conflict Decision. NO ``ship``: an unmerged
    # conflict has no verified artifact to ship past. ``retry`` re-opens the run
    # with the founder's guidance (RUNNING → OPEN, the agent re-resolves); the
    # merge-watch loop re-freshens the re-pushed head and merges. ``discard``
    # abandons the run → CANCELLED, and the merge-watch worker closes the now-
    # orphaned PR on its next poll (it observes the cancelled originating run).
    "merge_conflict_review": [
        DecisionAction(key=ACTION_RETRY, label_en="Guide & retry", label_ko="지침 주고 다시 시도"),
        DecisionAction(key=ACTION_DISCARD, label_en="Discard", label_ko="폐기"),
    ],
    # The merge watch gave up on an open PR. Its run already SHIPPED (the
    # deliverable landed and the founder approved it) — so this Decision is a
    # report, not a fork in the work: there is nothing to ship past, nothing to
    # re-drive, and discarding would cancel a run that genuinely shipped. The
    # remedy lives on GitHub; the one honest in-app action is to fold it away.
    "merge_watch_stalled": [
        DecisionAction(key=ACTION_ACKNOWLEDGE, label_en="Got it", label_ko="확인했어요"),
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
    reason = ""
    if isinstance(payload, dict):
        value = payload.get("question")
        if isinstance(value, str) and value.strip():
            return value
        reason = str(payload.get("reason") or "")
    # A reason-specific line wins over the kind's generic one, for a kind with
    # several distinct ways to arrive (``merge_watch_stalled``): the founder
    # reads what actually happened instead of a catch-all.
    variants = _EXECUTOR_DECISION_REASON_QUESTIONS.get(reason) or _EXECUTOR_DECISION_QUESTIONS.get(
        decision.decision
    )
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
    "ACTION_ACKNOWLEDGE",
    "ACTION_DISCARD",
    "ACTION_RETRY",
    "ACTION_SHIP",
    "DecisionAction",
    "_decision_actions",
    "_decision_options",
    "_question_text",
]
