"""Executor verification convergence (B2b) — the native-parity verify branch.

Extracted from :mod:`backend.executors.orchestrator` in Lift D (§17.8 4-file
split). Before B2b the executor trusted the worker's exit-0 signal and stamped
``ProofState.PROVED`` UNCONDITIONALLY. That is RETIRED. On worker success the
coordinator now acquires a sandbox mounting the run dir (where B1 persisted the
worker's files), assembles the shared :class:`VerificationContract`, and runs
the SAME verification the native loop runs — PROVED is set ONLY on a passing
:class:`VerificationResult`.

The HONEST branches (never a fake PROVED):

* no usable contract → ``human_review_required`` Decision.
* contract has judge checks but no verify LLM → ``human_review_required``.
* contract PASSES → verified Deliverable + PROVED.
* contract FAILS → ``verification_failed`` Decision (executor is
  single-dispatch — FAIL goes to the founder, NOT an auto-retry, NOT PROVED).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution.audit_events import LoopTerminal, VerifyRun
from backend.execution.db import (
    ExecutionRun,
    ProofState,
    RunAttempt,
    RunAttemptPhase,
    VerificationOutcome,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.verified_deliverable import write_verified_deliverable
from backend.execution.verifier.service import CanonRetriever, JudgeLlm, VerificationService
from backend.executors.terminal import (
    _utcnow,
    create_decision,
    decision_terminal,
    emit_audit,
    fail_terminal,
)
from backend.supervisor.sandbox import SandboxManager
from backend.workflow.application.agent_loop import LoopResult

logger = structlog.get_logger(__name__)

# Decision kinds raised by the B2b verification-convergence branch. These mirror
# the native loop's kinds so the founder surface is uniform across compute
# backends (the native ``_drive_loop`` raises the same two).
DECISION_HUMAN_REVIEW_REQUIRED = "human_review_required"
DECISION_VERIFICATION_FAILED = "verification_failed"


class _UnavailableJudge:
    """A :class:`JudgeLlm` sentinel for when no verification LLM is available.

    Never reached at runtime: a judge-bearing contract is routed to a
    human-review Decision before ``verify`` runs, and a command-only contract
    makes no judge call. If a judge call is ever attempted it raises loudly
    rather than silently passing — a refused judge is never a silent pass."""

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> Any:
        raise RuntimeError("no verification LLM available for the judge")


async def verify_and_finish(
    *,
    session: AsyncSession,
    sandbox_manager: SandboxManager,
    retriever: CanonRetriever | None,
    verify_llm: JudgeLlm | None,
    run: ExecutionRun,
    work_step: WorkStep,
    attempt: RunAttempt,
    task_id: uuid.UUID,
    artifact_refs: list[str],
    output: str,
    workspace_dir: Path,
) -> LoopResult:
    """Verify the worker's captured artifacts and land an HONEST terminal.

    Acquires a sandbox mounting the run dir (where B1 persisted the files),
    assembles + runs the shared verification contract, and branches WITHOUT
    ever faking PROVED. A sandbox/verify hiccup degrades to ``system_error``
    (true infra failure) — never a silent verified, never a crash that leaks
    the loop (mirrors the native orchestrator's soft-fail discipline).
    """
    attempt.phase = RunAttemptPhase.VERIFYING
    await session.flush()

    # The sandbox is keyed on the run's project the same way native derives
    # it (``run.product_id or run.id``) and mounts the run dir B1 wrote into.
    project_id = run.product_id or run.id
    try:
        box = await sandbox_manager.acquire(project_id, str(workspace_dir))
    except Exception as exc:  # noqa: BLE001 — infra failure → system_error
        logger.warning(
            "executor_orchestrator_sandbox_unavailable", run_id=str(run.id), error=str(exc)
        )
        return await fail_terminal(
            session, run, work_step, attempt, summary=f"sandbox unavailable: {exc}"
        )

    try:
        # The service requires a ``JudgeLlm``; when none is available we pass
        # a sentinel that raises if a judge call is ever attempted. It is
        # NEVER reached: a judge-bearing contract is routed to human review
        # below before ``verify`` runs, and command-only contracts make no
        # judge call (asserted by the VerificationService unit tests).
        judge: JudgeLlm = verify_llm or _UnavailableJudge()
        svc = VerificationService(session=session, llm=judge, retriever=retriever)
        contract = await svc.assemble_contract(
            declared_contract=None,
            written_paths=artifact_refs,
            final_text=output,
        )

        # HONEST branch 1 — no usable contract → human review (NOT a silent
        # pass, NOT PROVED). This is the anti-regression for the fake-PROVED
        # sin: exit-0 with nothing to check is NOT a verified deliverable.
        if contract is None:
            decision = await create_decision(
                session,
                run,
                kind=DECISION_HUMAN_REVIEW_REQUIRED,
                rationale="executor produced work but there is no verifiable contract",
                payload={"reason": "no_verifiable_contract", "artifact_refs": artifact_refs},
            )
            return await decision_terminal(session, run, work_step, attempt, decision)

        # HONEST branch 2 — the contract has judge checks but no verify LLM is
        # available (e.g. executor-only-active workspace → no resolvable judge
        # account). Command-only contracts still run; a judge-bearing contract
        # we cannot grade routes to human review (NOT PROVED).
        if contract.judge_checks and verify_llm is None:
            decision = await create_decision(
                session,
                run,
                kind=DECISION_HUMAN_REVIEW_REQUIRED,
                rationale="contract requires an LLM judge but no verification LLM is available",
                payload={"reason": "no_verification_llm", "artifact_refs": artifact_refs},
            )
            return await decision_terminal(session, run, work_step, attempt, decision)

        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=artifact_refs,
            final_text=output,
        )
        # B15 — VerifyRun: outcome + check counts (no result body).
        await emit_audit(
            session,
            run,
            attempt,
            VerifyRun,
            {
                "outcome": vr.outcome.value,
                "command_checks": len(contract.command_checks),
                "judge_checks": len(contract.judge_checks),
            },
        )
    except Exception as exc:  # noqa: BLE001 — any verify crash → system_error
        logger.exception("executor_orchestrator_verify_crash", run_id=str(run.id))
        return await fail_terminal(
            session, run, work_step, attempt, summary=f"verification crashed: {exc}"
        )
    finally:
        try:
            await sandbox_manager.release(project_id)
        except Exception:  # noqa: BLE001 — release best-effort, never leak
            logger.warning(
                "executor_orchestrator_sandbox_release_failed",
                run_id=str(run.id),
                exc_info=True,
            )

    # HONEST branch 3 — verification FAILED → founder Decision. Executor is
    # single-dispatch (no agent loop to replan), so FAIL goes to the founder,
    # NOT an auto-retry, NOT PROVED.
    if vr.outcome is not VerificationOutcome.PASSED:
        decision = await create_decision(
            session,
            run,
            kind=DECISION_VERIFICATION_FAILED,
            rationale="executor work failed the verification contract",
            payload={"artifact_refs": artifact_refs, "verification_result_id": str(vr.id)},
        )
        return await decision_terminal(session, run, work_step, attempt, decision)

    # The ONLY PROVED path — gated on a real passing VerificationResult,
    # exactly like the native ``_finish_verified``.
    work_step.status = WorkStepStatus.VERIFIED
    work_step.proof_state = ProofState.PROVED
    attempt.phase = RunAttemptPhase.COMPLETED
    attempt.finished_at = _utcnow()
    deliverable = await write_verified_deliverable(
        session,
        run,
        attempt_id=attempt.id,
        artifact_refs=artifact_refs,
        summary=output,
    )
    logger.info(
        "executor_orchestrator_verified",
        run_id=str(run.id),
        task_id=str(task_id),
        deliverable_id=str(deliverable.id),
        verification_result_id=str(vr.id),
        artifact_refs=artifact_refs,
    )
    # B15 — terminal (verified) — the founder-facing closing event.
    await emit_audit(
        session,
        run,
        attempt,
        LoopTerminal,
        {"outcome": "verified", "written_paths_count": len(artifact_refs)},
    )
    return LoopResult(
        outcome="verified",
        run_id=run.id,
        work_step_id=work_step.id,
        run_attempt_id=attempt.id,
        verification_result_id=vr.id,
        written_paths=artifact_refs,
        summary=output,
    )


__all__ = [
    "DECISION_HUMAN_REVIEW_REQUIRED",
    "DECISION_VERIFICATION_FAILED",
    "verify_and_finish",
]
