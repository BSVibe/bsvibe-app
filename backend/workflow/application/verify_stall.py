"""When a failed verification is not something another round can change.

Two shapes, both of which used to be handed straight back to the agent:

* ``NO_FAILURE_FOUND`` — nothing reported a failure, so there is nothing for the agent to fix; the
  fault is the harness's, and only the founder can say what to do about it.
* the SAME failure three times running — the agent has seen it, been offered the stuck-review hint
  (which fires on the second sighting), and the round after that did not move it either;
* the agent's WORK not moving — the tree is byte-identical to what last failed, three rounds
  running. The failure text cannot say this on its own: the gate's commands are re-derived by a
  model every round and their wording drifts (prod #1204: 8 rounds, one file written, never a
  "repeat"), but no verdict changes because of work that did not change.

Either way the loop owes the founder a question rather than another turn (a run once spent its rounds
on a gate its own request had implied and its founder had already overruled).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from backend.workflow.application.audit_events import DecisionPending, LoopTerminal
from backend.workflow.application.round_budget import finalize_round_cap_decision
from backend.workflow.domain.verification_feedback import NO_FAILURE_FOUND, failure_signature

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import LoopResult, RunOrchestrator
    from backend.workflow.infrastructure.db import Decision, ExecutionRun, RunAttempt, WorkStep
    from backend.workflow.infrastructure.sandbox import SandboxSession

NO_FAILURE = "no_failure_found"
REPEATED_FAILURE = "repeated_failure"
NO_PROGRESS = "no_progress"

#: Repeats of a failure (beyond its first sighting) the agent is allowed before the founder is
#: asked. Two, not one: the second sighting is where ``stuck_review_hint`` offers the agent a
#: second opinion, and escalating there would pre-empt the very remedy meant to unstick it.
REPEAT_LIMIT = 2

_CONTEXT_CLIP = 1_500


@dataclass
class StallTracker:
    """How many failed rounds running nothing moved — the failure, or the work.

    A round repeats the last when its failure signature is the same OR the tree is the same as
    when the last one failed (``tree`` from :func:`tree_fingerprint`; ``None`` = unknown, then
    only the signature counts). ``unchanged`` says which: the founder's question differs.
    """

    repeats: int = 0
    unchanged: bool = False
    _signature: tuple[Any, ...] | None = None
    _tree: str | None = None

    def observe(self, result: dict[str, Any], *, tree: str | None) -> int:
        signature = failure_signature(result)
        same_tree = tree is not None and tree == self._tree
        self.repeats = self.repeats + 1 if (same_tree or signature == self._signature) else 0
        self.unchanged = same_tree
        self._signature, self._tree = signature, tree
        return self.repeats


async def tree_fingerprint(box: SandboxSession, baseline: str | None) -> str | None:
    """A hash of the work as git sees it — tracked changes since ``baseline`` (or ``HEAD``) and
    every untracked file's content. ``None`` when git cannot answer (not a repo)."""
    base = baseline or "HEAD"
    command = (
        "git -c core.quotePath=false status --porcelain --untracked-files=all"
        f" && git -c core.quotePath=false diff {base}"
        " && git ls-files -o --exclude-standard | git hash-object --stdin-paths"
    )
    try:
        # S604: the sandbox Protocol's own flag; ``base`` is a commit id or ``HEAD``.
        result = await box.exec(command, timeout_s=30.0, shell=True)  # noqa: S604
    except Exception:  # noqa: BLE001 — an unanswerable tree is unknown, not unchanged
        return None
    if result.exit_code != 0 or result.timed_out:
        return None
    return hashlib.sha256((result.stdout or "").encode()).hexdigest()


def stall_question(failure: str, *, repeats: int, unchanged: bool = False) -> dict[str, str] | None:
    """The Decision payload for a verification the agent cannot move, or ``None``.

    ``failure`` is the rendered feedback of THIS round, ``repeats`` how many rounds running it has
    already come back the same (:class:`StallTracker`; 0 on first sight), ``unchanged`` whether
    that was the work standing still rather than the failure text.
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
    if repeats >= REPEAT_LIMIT and unchanged:
        return {
            "reason": NO_PROGRESS,
            "question": (
                "작업이 더 바뀌지 않는데 검증이 계속 실패합니다 — 코드가 아니라 검증 환경 "
                "(설치·도구·권한)의 문제일 수 있습니다. 실패 내용을 보고 어떻게 할지 정해 주세요."
            ),
            "context": failure[:_CONTEXT_CLIP],
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
    "NO_PROGRESS",
    "REPEATED_FAILURE",
    "REPEAT_LIMIT",
    "StallTracker",
    "failure_signature",
    "settle_stalled_verification",
    "stall_question",
    "tree_fingerprint",
]
