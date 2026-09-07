"""Loop ``request_review`` — a clean-context second opinion for a stuck agent.

prod measured a stuck agent repeat the SAME failing approach instead of changing it:

* run ``010bbdd8`` — code comment records "repeated the identical failure 16 times"
* run ``40c14a10`` — 8 consecutive verification failures, 38 minutes burned, cancelled
* run ``0093fce6`` — round budget spent on 2 failures

The loop's own failure message is just "Fix the problem and try again" (see the
``messages.append`` at the bottom of :func:`drive_loop` in ``_drive_loop.py``) — nothing
in it prompts the agent to change APPROACH, because the agent that keeps failing is the
same agent reading its own prior (failing) reasoning back to itself.

This module gives the loop's own LLM ONE extra move: hand the run's actual
``VerificationResult`` history (every attempt, not just the last) to a review turn that
carries NONE of the working turns' context, and return its plain-text feedback as a tool
result. v1 scope, deliberately: no new agentic loop, no sandbox, no tools of its own —
just one :meth:`LoopLlm.complete` call over the SAME ``orch._llm`` the drive loop already
holds. That is what keeps this a bounded, reviewable change instead of a second execution
engine.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.domain.verification_feedback import render_verification_failure
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    VerificationOutcome,
    VerificationResult,
)

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import LoopLlm, LoopToolCall, RunOrchestrator
    from backend.workflow.infrastructure.db import RunAttempt

logger = structlog.get_logger(__name__)

REQUEST_REVIEW_NAME = "request_review"

#: A review is a full LLM completion over the run's whole failure history — real cost,
#: spent on top of the turn budget. It exists to break ONE blind spot, not to become a
#: subroutine a stuck agent leans on instead of fixing the bug. Mirrors the existing
#: ``MAX_NO_WORK_NUDGES = 2`` budget in ``tool_registry.py`` for the same shape of reason:
#: one call to get a different angle, one more to confirm the new approach actually
#: helped. A third call with no progress means the review itself isn't unsticking it —
#: ``ask_user_question`` exists for exactly that, and costs nothing to call.
MAX_STUCK_REVIEWS_PER_RUN = 2

#: Where the run remembers how many reviews it has already spent, persisted on
#: ``ExecutionRun.payload`` — NOT just a local variable inside ``drive_loop`` — so the cap
#: survives a round-budget-exhausted retry starting a FRESH ``drive_loop`` call. Same reason
#: ``round_budget_exhausted`` is persisted there rather than kept in a loop-local (see the
#: PR #889 comment in ``_drive_loop.py``): a fresh loop instance must not forget what an
#: earlier instance of the SAME run already spent.
STUCK_REVIEW_STATE_KEY = "stuck_review_calls"

#: Bounds the PROMPT, not the gate: only the most recent N attempts are shown to the
#: reviewer. Repetition is visible well within this window, and an unbounded history would
#: blow the completion's context budget on a long-running stuck run.
_MAX_ATTEMPTS_IN_HISTORY = 12

_NO_HISTORY_MESSAGE = (
    "request_review refused: this run has not attempted verification yet, so there is no "
    "attempt history for a reviewer to read. Declare your checks and make a real attempt "
    "first — a reviewer with nothing to look at cannot help you."
)


def _budget_exhausted_message(limit: int) -> str:
    return (
        f"request_review refused: this run already used its {limit} review call(s). "
        "Calling it again will not produce a different answer — either try a genuinely "
        "different approach yourself, or use ask_user_question to bring in the founder."
    )


_REVIEWER_SYSTEM_PROMPT = (
    "You are reviewing a CODING AGENT that is stuck: across this run it has attempted "
    "verification more than once and keeps failing. You have not tried anything yourself "
    "and have no attachment to its prior choices — that is the whole point of asking you. "
    "Below is the run's FULL verification attempt history, oldest first, not just the "
    "latest failure. Look for a PATTERN across the attempts: is it repeating the same fix? "
    "Misreading the error? Editing the wrong file? Fixing a symptom instead of the cause? "
    "Reply with 2-4 concrete sentences naming the likely root cause and a genuinely "
    "DIFFERENT next step. Do not just say 'fix the error' — that is what has already failed."
)

REQUEST_REVIEW_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REQUEST_REVIEW_NAME,
        "description": (
            "Ask a reviewer with NO attachment to your prior attempts to look at this run's "
            "actual verification failure history — every attempt so far, not just the last "
            "one — and suggest what to try differently. Call this when you have already "
            "tried to fix a failure at least once and verification is STILL failing. Refused "
            "if you have not attempted verification yet (nothing to review), and capped at "
            f"{MAX_STUCK_REVIEWS_PER_RUN} calls for this run (a review is a real LLM call; if "
            "it hasn't unstuck you by then, ask the founder with ask_user_question instead of "
            "calling this again)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": (
                        "Optional — anything not already visible in the failure history: "
                        "what you were trying, your current hypothesis."
                    ),
                },
            },
            "required": [],
        },
    },
}


def _format_attempt(index: int, total: int, row: VerificationResult) -> str:
    header = f"--- Attempt {index}/{total} ({row.outcome.value}) ---"
    if row.outcome is VerificationOutcome.PASSED:
        return f"{header}\n(this attempt passed)"
    result = row.result if isinstance(row.result, dict) else {}
    return f"{header}\n{render_verification_failure(result)}"


def format_attempt_history(rows: Sequence[VerificationResult]) -> str:
    """The run's OWN recorded attempts, oldest first — the reviewer's one primary source.

    Deliberately not the agent's own narration of what it tried: the ``VerificationResult``
    rows are the run's ground truth of what was actually attempted, independent of how the
    agent describes its own attempts to itself.
    """
    total = len(rows)
    return "\n\n".join(_format_attempt(i, total, row) for i, row in enumerate(rows, start=1))


def _review_messages(history_text: str, extra_context: str) -> list[dict[str, Any]]:
    user_parts = [f"Verification attempt history for this run:\n\n{history_text}"]
    if extra_context:
        user_parts.append(f"The agent adds:\n{extra_context}")
    return [
        {"role": "system", "content": _REVIEWER_SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


async def handle_request_review(
    session: AsyncSession,
    run: ExecutionRun,
    llm: LoopLlm,
    arguments: dict[str, Any],
) -> str:
    """Handle the loop-owned ``request_review`` pseudo-tool call.

    Two refusals, both BEFORE any LLM spend (the negative controls this tool exists to
    prove): no verification attempt yet, and the per-run call budget already spent. Both are
    returned as ordinary tool output — plain text the agent reads on its next turn — rather
    than raised, so a stuck agent that calls this too eagerly is told why instead of having
    its turn crash.
    """
    rows = list(
        (
            await session.execute(
                select(VerificationResult)
                .where(VerificationResult.run_id == run.id)
                .order_by(VerificationResult.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return _NO_HISTORY_MESSAGE

    used = int((run.payload or {}).get(STUCK_REVIEW_STATE_KEY, 0))
    if used >= MAX_STUCK_REVIEWS_PER_RUN:
        return _budget_exhausted_message(MAX_STUCK_REVIEWS_PER_RUN)

    # Spend the budget slot BEFORE the (possibly slow, possibly failing) LLM call, and
    # COMMIT now rather than leaving it in the loop's open transaction — the same
    # "release the connection before external work" discipline ``drive_loop`` uses around
    # its own sandbox / LLM calls (see the COMMIT FIRST comment in ``_drive_loop.py``). The
    # cost here is incurred by MAKING the request, not by getting a useful answer back, so
    # a reviewer call that errors out still counts — otherwise an agent could spin on a
    # broken reviewer forever without ever tripping the cap.
    run.payload = {**(run.payload or {}), STUCK_REVIEW_STATE_KEY: used + 1}
    await session.commit()

    history_text = format_attempt_history(rows[-_MAX_ATTEMPTS_IN_HISTORY:])
    extra_context = str(arguments.get("context") or "").strip()
    try:
        turn = await llm.complete(
            messages=_review_messages(history_text, extra_context), tools=None
        )
    except Exception:  # noqa: BLE001 — a review hiccup must never crash the loop
        logger.warning("request_review_failed", run_id=str(run.id), exc_info=True)
        return "request_review failed: the reviewer could not be reached. Proceed on your own judgement."
    feedback = (turn.content or "").strip()
    return feedback or "The reviewer had no specific feedback."


async def handle_review_call(
    orch: RunOrchestrator, run: ExecutionRun, attempt: RunAttempt, call: LoopToolCall
) -> dict[str, Any]:
    """One ``request_review`` tool call end to end: gate + review + activity log, returning
    the ``role: tool`` message the loop appends. Pulled out of ``_drive_loop.py`` (rather
    than inlined like ``emit_deliverable``'s handling there) to keep that file under its
    600 LOC ceiling (v8 §17.1) — the same reason ``_loop_context.py`` exists.
    """
    output = await handle_request_review(orch._session, run, orch._llm, call.arguments)
    await orch._record(run, attempt, "tool_call", {"tool": call.name, "ok": True, "writes": []})
    return {"role": "tool", "tool_call_id": call.id, "content": output}


__all__ = [
    "MAX_STUCK_REVIEWS_PER_RUN",
    "REQUEST_REVIEW_NAME",
    "REQUEST_REVIEW_TOOL",
    "STUCK_REVIEW_STATE_KEY",
    "format_attempt_history",
    "handle_request_review",
    "handle_review_call",
]
