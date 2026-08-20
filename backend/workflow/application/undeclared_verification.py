"""선언이 0개일 때의 판정 — 통과냐 실패냐, 제3상태는 없다.

형님 판정 2026-08-20: *"검증은 통과/실패 둘 뿐이야 오직. 이 때 실패와 명시적 없음은
달라. 검증 할게 있었는데 실패한건 실패한거고. 정말 검증할게 없어서 아무것도 안한거는
통과야."*

``assemble_contract`` 이 ``None`` 을 내는 자리 — 에이전트가 검사를 하나도 선언하지 않은
런 — 는 지금까지 ``human_review_required`` / ``no_verification_declared`` Decision 을
세워 런을 형님 위에 파킹했다. 그것이 판정을 이원화하면 남지 않아야 할 제3상태다.

이 모듈은 그 자리 하나만 소유한다. ``inplace_gate`` 가 client_attach 의 settle 결정
하나만 소유하는 것과 같은 모양이고, ``_drive_loop`` 를 600 LOC 천장 아래로 유지한다.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from backend.workflow.application.audit_events import LoopTerminal
from backend.workflow.application.inplace_gate import changed_paths
from backend.workflow.domain.verifier_contract import VerificationContract
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    RunAttempt,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
)
from backend.workflow.infrastructure.sandbox import SandboxSession

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import LoopResult, RunOrchestrator


#: How many changed paths the "you must declare" message names before it says so.
#: A silent cap reads as a complete list — the exact shape that made a truncated
#: deliverable summary fabricate a number (#722).
_MAX_NAMED_PATHS = 40


async def settle_undeclared_verification(
    orch: RunOrchestrator,
    *,
    run: ExecutionRun,
    work_step: WorkStep,
    attempt: RunAttempt,
    box: SandboxSession,
    baseline: str | None,
    written_paths: list[str],
    final_text: str,
    messages: list[dict[str, Any]],
    knowledge: Any = None,
) -> LoopResult | None:
    """The agent declared no way to prove its work. Pass or fail — never a third state.

    Founder ruling 2026-08-20: *"검증은 통과/실패 둘 뿐이야 오직. 이 때 실패와 명시적
    없음은 달라. 검증 할게 있었는데 실패한건 실패한거고. 정말 검증할게 없어서 아무것도
    안한거는 통과야."* The line is not "was there a gate" but **"was there anything to
    prove"**. This site used to raise a ``human_review_required`` /
    ``no_verification_declared`` Decision and park the run on the founder — exactly the
    third state the ruling removes.

    Returns the verified terminal (nothing was changed, so nothing was owed), or ``None``
    to send the cycle round again with the agent now owing a declaration. ``None`` is not
    an open loop: the round cap already owns the terminal.

    **Why git and not ``written_paths``.** The B7 verify-first gate holds back
    ``file_write``/``file_edit`` but not ``shell_exec``, so an agent can do all of its work
    through the shell and the server records no writes at all — prod ``fae09a47``:
    ``shell_exec`` 62 times, every activity ``writes`` empty, and a ``+108/−2`` commit.
    Judged on what the server saw, that run "changed nothing" and would pass here with
    zero checks run. :func:`changed_paths` asks the tree, which cannot be fooled that way,
    and its own docstring already names this use: *"did this run change anything at all"*.

    The two are UNIONed rather than either one trusted alone. A git probe that fails
    contributes nothing (by design — it must never guess), so on its own an unreachable
    tree is indistinguishable from a clean one, and "no answer" would drain into a pass:
    the costume a wiring break wore in the 2026-08-09 E2E. Writes the server DID see keep
    that case honest. The union can only over-report, and over-reporting costs a
    declaration the agent should have made anyway.
    """
    # An external call follows. Holding the pooled connection across it is the recurring
    # outage (#632/#686/#680): the work succeeds and the RECORDING dies.
    await orch._session.commit()
    changed = sorted(set(await changed_paths(box, baseline)) | set(written_paths))

    if changed:
        named = changed[:_MAX_NAMED_PATHS]
        elided = len(changed) - len(named)
        # Last and most specific sentence wins (#779, and again #781 — a paragraph
        # prepended to a winning instruction loses to it). This is appended at the END
        # of the conversation, which is why it says what to DO, not what went wrong.
        messages.append(
            {
                "role": "user",
                "content": (
                    "This run CHANGED files but declared no way to prove them. "
                    "Verification is pass or fail — there is no third state, so a change "
                    "nobody can check cannot land.\n"
                    "Changed in this run:\n"
                    + "\n".join(f"- {path}" for path in named)
                    + (f"\n- (+{elided} more)" if elided else "")
                    + "\nCall declare_verification with the commands that prove THIS "
                    "change, run them, then send your summary."
                ),
            }
        )
        return None

    # Nothing was changed, so nothing was owed. Persist the pass as an inspectable
    # record: ``_finish_verified`` puts this row's id on the LoopResult and builds the
    # deliverable summary from its ``result``.
    verdict = VerificationResult(
        id=uuid.uuid4(),
        run_id=run.id,
        work_step_id=work_step.id,
        workspace_id=run.workspace_id,
        outcome=VerificationOutcome.PASSED,
        contract=VerificationContract(checks=()).to_dict(),
        result={
            "command_results": [],
            "derived_gate": None,
            # The SAME question ``verify`` asks — does the repo-gate concept apply here
            # at all. ``_weak_evidence_sentence`` reads it to decide whether the founder
            # hears that this pass rests on nothing that ran (#742: a weak finish that
            # never reaches them is one they never hear about).
            "gate_applicable": run.product_id is not None
            and orch._verifier()._is_real_worktree(run),
            # ``gate_expected`` is deliberately ABSENT: it means "a manifest exists, so a
            # deriver that could not run is a failure". No deriver was owed here, so the
            # question was never asked, and writing either answer would be a claim the
            # run cannot support.
            "undeclared_no_change": True,
        },
    )
    orch._session.add(verdict)
    result = await orch._finish_verified(
        run, work_step, attempt, written_paths, final_text, verdict, knowledge
    )
    await orch._audit(
        run, attempt, LoopTerminal, {"outcome": "verified", "undeclared_no_change": True}
    )
    return result


__all__ = ["settle_undeclared_verification"]
