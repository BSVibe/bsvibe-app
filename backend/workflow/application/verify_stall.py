"""When a failed verification is not something another round can change.

Two shapes, both of which used to be handed straight back to the agent:

* ``NO_FAILURE_FOUND`` — nothing reported a failure, so there is nothing for the agent to fix; the
  fault is the harness's, and only the founder can say what to do about it.
* the SAME failure three times running — the agent has seen it, been offered the stuck-review hint
  (which fires on the second sighting), and the round after that did not move it either.

Either way the loop owes the founder a question rather than another turn (f7c5b4d1 spent its rounds
on a gate its own request had implied and its founder had already overruled).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.workflow.application.audit_events import DecisionPending, LoopTerminal
from backend.workflow.application.round_budget import finalize_round_cap_decision
from backend.workflow.domain.verification_feedback import NO_FAILURE_FOUND, failure_signature

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import LoopResult, RunOrchestrator
    from backend.workflow.infrastructure.db import Decision, ExecutionRun, RunAttempt, WorkStep

NO_FAILURE = "no_failure_found"
REPEATED_FAILURE = "repeated_failure"

#: Repeats of a failure (beyond its first sighting) the agent is allowed before the founder is
#: asked. Two, not one: the second sighting is where ``stuck_review_hint`` offers the agent a
#: second opinion, and escalating there would pre-empt the very remedy meant to unstick it.
REPEAT_LIMIT = 2

_CONTEXT_CLIP = 1_500


def stall_question(failure: str, *, repeats: int) -> dict[str, str] | None:
    """The Decision payload for a verification the agent cannot move, or ``None``.

    ``failure`` is the rendered feedback of THIS round, ``repeats`` how many rounds running it has
    already come back the same failure (``failure_signature``; 0 on first sight).
    """
    if failure == NO_FAILURE_FOUND:
        return {
            "reason": NO_FAILURE,
            "question": (
                "검증이 실패로 끝났는데 실패를 보고한 검사가 하나도 없습니다. "
                "작업이 아니라 검증 하네스의 문제로 보입니다 — 어떻게 진행할까요?"
            ),
            "context": failure,
        }
    if repeats >= REPEAT_LIMIT:
        return {
            "reason": REPEATED_FAILURE,
            "question": (
                "같은 검증 실패가 에이전트의 수정 뒤에도 그대로 반복됩니다. "
                "검증 기준을 바꾸거나 작업 방향을 정해 주세요."
            ),
            "context": failure[:_CONTEXT_CLIP],
        }
    return None


async def settle_stalled_verification(
    orch: RunOrchestrator,
    *,
    run: ExecutionRun,
    work_step: WorkStep,
    attempt: RunAttempt,
    stalled: dict[str, str],
    written_paths: list[str],
    final_text: str,
    stats: dict[str, int],
) -> LoopResult:
    """Pause the run on a Decision for a stalled verification.

    A repeated failure takes the grounded deliverable choice the round cap already builds
    (skip that part / show the rest), since that is exactly the situation it describes — only
    reached sooner. A round with no failure at all has nothing to ground a choice in, so it asks
    the plain question.
    """
    if stalled["reason"] == REPEATED_FAILURE:
        decision: Decision = await finalize_round_cap_decision(
            orch,
            run=run,
            work_step=work_step,
            attempt=attempt,
            written_paths=written_paths,
            stats=stats,
        )
    else:
        decision = await orch._create_decision(
            run,
            work_step,
            kind="ask_user_question",
            payload=stalled,
            rationale="verification failed with no check reporting a failure",
        )
        await orch._audit(
            run,
            attempt,
            DecisionPending,
            {
                "kind": "ask_user_question",
                "decision_id": str(decision.id),
                "reason": stalled["reason"],
                "question": stalled["question"],
            },
        )
    await orch._audit(
        run, attempt, LoopTerminal, {"outcome": "needs_decision", "decision_id": str(decision.id)}
    )
    return orch._decision_result(run, work_step, attempt, decision, written_paths, final_text)


__all__ = [
    "NO_FAILURE",
    "REPEATED_FAILURE",
    "REPEAT_LIMIT",
    "failure_signature",
    "settle_stalled_verification",
    "stall_question",
]
