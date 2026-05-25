"""ExecutorOrchestrator — drive an ExecutionRun via an external CLI worker.

Lift 5b of the executor-pool epic (Workflow §8.4 / §11.3). The KEYSTONE: a run
whose resolved ModelAccount is ``provider='executor'`` must NOT enter the native
plan→act→verify LLM loop (:class:`~backend.execution.orchestrator.RunOrchestrator`).
Instead it dispatches a single task to a registered external worker (a remote
machine running a CLI coding agent — ``claude_code`` / ``codex`` / ``opencode``)
and, on the worker reporting success, lands the SAME verified artifacts the
native path produces (Deliverable type CODE + DeliveryEventRow + settle
activity, via the shared :func:`write_verified_deliverable` helper).

It satisfies the :class:`~backend.execution.orchestrator.RunCompute` Protocol —
same ``run(*, run, workspace_dir) -> LoopResult`` signature as the native
orchestrator — so :class:`~backend.orchestrator.agent_runner.AgentRunner.drive`
maps its outcome identically: ``verified → REVIEW_READY``,
``system_error → FAILED``, ``needs_decision`` leaves the run RUNNING (paused on
a :class:`Decision`).

Flow:

1. Resolve the executor identity from ``account.extra_params``
   (``executor_type`` + optional pinned ``worker_id``).
2. Frame the CLI prompt from the run's intent text (the same stable input the
   native loop seeds its first user turn with — never a guessed/LLM value).
3. Pick a worker via :func:`dispatch.find_available_worker` (the pinned worker
   wins when online). NONE available (or no Redis client to dispatch over) →
   create a Decision, return ``needs_decision`` (stuck → Decision, never a
   silent stall).
4. :func:`dispatch.create_task` → :func:`dispatch.dispatch_task` →
   :func:`dispatch.await_completion` (timeout = ``settings.executor_task_timeout_s``).
5. On success → run the SAME verification the native loop runs (B2b: assemble
   contract + verify in a sandbox) and ONLY set PROVED on a passing
   VerificationResult; on failure / timeout → ``system_error``.

B2b (executor verification convergence) — native full convergence. The old v1
trusted the worker's success signal and set ``ProofState.PROVED`` /
``WorkStepStatus.VERIFIED`` UNCONDITIONALLY on exit-0 (a hollow "verified"
Deliverable). That is RETIRED. On worker success the orchestrator now:

* acquires a sandbox mounting the run dir (where B1 persisted the worker's
  files, ``run_workspace_root/<run_id>/``),
* assembles a :class:`VerificationContract` via the shared
  :class:`~backend.execution.verifier.service.VerificationService` (declared +
  BSage canon retrieval), and
* verifies it. PROVED is set on, and ONLY on, a passing
  :class:`VerificationResult` — exactly like the native ``_finish_verified``.

The HONEST branches (never a fake PROVED):

* no usable contract → ``human_review_required`` Decision (reason
  ``no_verifiable_contract``).
* contract has judge checks but no verify LLM → ``human_review_required``
  (reason ``no_verification_llm``); command-only contracts still run.
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

from backend.accounts.models import ModelAccount
from backend.config import Settings, get_settings
from backend.execution.db import (
    Decision,
    ExecutionRun,
    ProofState,
    RunAttempt,
    RunAttemptPhase,
    VerificationOutcome,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.orchestrator import LoopResult
from backend.execution.verified_deliverable import write_verified_deliverable
from backend.execution.verifier.service import CanonRetriever, JudgeLlm, VerificationService
from backend.executors import dispatch
from backend.executors.dispatch import TaskTimeout
from backend.supervisor.sandbox import SandboxManager

logger = structlog.get_logger(__name__)

# Decision kinds raised when an executor run cannot dispatch.
DECISION_NO_WORKER_AVAILABLE = "no_executor_worker_available"
DECISION_NO_DISPATCH_TRANSPORT = "no_executor_dispatch_transport"
# Decision kinds raised by the B2b verification-convergence branch. These mirror
# the native loop's kinds so the founder surface is uniform across compute
# backends (the native ``_drive_loop`` raises the same two).
DECISION_HUMAN_REVIEW_REQUIRED = "human_review_required"
DECISION_VERIFICATION_FAILED = "verification_failed"


def _intent_text(run: ExecutionRun) -> str:
    """The run's framed instruction — the same stable input the native loop
    seeds its first user turn with (``backend.execution.orchestrator._intent_title``)."""
    payload = run.payload or {}
    text = payload.get("intent_text") or payload.get("text") or "Untitled run"
    return str(text)[:512]


class ExecutorOrchestrator:
    """Drive one run by dispatching to an external CLI worker (RunCompute)."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        redis: Any,
        account: ModelAccount,
        sandbox_manager: SandboxManager,
        settings: Settings | None = None,
        retriever: CanonRetriever | None = None,
        verify_llm: JudgeLlm | None = None,
    ) -> None:
        self._session = session
        self._redis = redis
        self._account = account
        self._settings = settings or get_settings()
        # B2b verification-convergence seams. ``sandbox_manager`` mounts the run
        # dir to run the contract's command checks; ``verify_llm`` grades judge
        # checks (None → judge-bearing contracts route to human review);
        # ``retriever`` folds BSage canon into the contract (None for now — B3
        # injects it later, this is a one-line wire).
        self._sandbox_manager = sandbox_manager
        self._retriever = retriever
        self._verify_llm = verify_llm

    async def run(self, *, run: ExecutionRun, workspace_dir: Path) -> LoopResult:
        work_step = WorkStep(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            title=_intent_text(run),
            status=WorkStepStatus.RUNNING,
            proof_state=ProofState.UNTESTED,
            payload={},
        )
        attempt = RunAttempt(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            phase=RunAttemptPhase.WORKING,
            payload={},
        )
        self._session.add_all([work_step, attempt])
        await self._session.flush()

        extra = self._account.extra_params or {}
        executor_type = str(extra.get("executor_type") or "")
        pinned_raw = extra.get("worker_id")
        pinned_worker_id = _parse_uuid(pinned_raw) if pinned_raw is not None else None

        # No Redis transport → no worker dispatch possible. Stuck → Decision
        # (never a silent stall): the worker daemon must run with a Redis client.
        if self._redis is None:
            decision = await self._create_decision(
                run,
                kind=DECISION_NO_DISPATCH_TRANSPORT,
                rationale="executor run has no Redis client to dispatch the worker task over",
                payload={"executor_type": executor_type},
            )
            return self._decision_result(run, work_step, attempt, decision)

        worker = await dispatch.find_available_worker(
            self._session,
            workspace_id=run.workspace_id,
            executor_type=executor_type,
            pinned_worker_id=pinned_worker_id,
        )
        if worker is None:
            decision = await self._create_decision(
                run,
                kind=DECISION_NO_WORKER_AVAILABLE,
                rationale=f"no online worker with capability '{executor_type}'",
                payload={
                    "executor_type": executor_type,
                    "pinned_worker_id": str(pinned_worker_id) if pinned_worker_id else None,
                },
            )
            return self._decision_result(run, work_step, attempt, decision)

        prompt = _intent_text(run)
        # NOTE: ``workspace_dir`` here is the BACKEND container's run path
        # (``/app/var/runs/<run_id>`` on the backend's appdata volume). A worker
        # is a SEPARATE machine where that absolute path does not exist, so we
        # must NOT ship it — sending it as the task cwd made claude-code fail to
        # chdir ([Errno 2] No such file or directory). The worker now creates and
        # manages its own isolated per-task local dir; we send a neutral ``"."``
        # (also ``create_task``'s default). The task carries ``run_id`` so the
        # worker's result path persists the files the CLI produced under this
        # run's workspace (``run_workspace_root/<run_id>/``) — surfaced as the
        # verified Deliverable's artifact_refs (executor-pool B1).
        task = await dispatch.create_task(
            self._session,
            workspace_id=run.workspace_id,
            executor_type=executor_type,
            prompt=prompt,
            workspace_dir=".",
            run_id=run.id,
        )
        await dispatch.dispatch_task(
            self._redis, session=self._session, task=task, worker_id=worker.id
        )
        logger.info(
            "executor_orchestrator_dispatched",
            run_id=str(run.id),
            task_id=str(task.id),
            worker_id=str(worker.id),
            executor_type=executor_type,
        )

        attempt.phase = RunAttemptPhase.VERIFYING
        await self._session.flush()

        # COMMIT the dispatched task before awaiting. The worker reports its
        # result over a SEPARATE session (the ``/result`` HTTP endpoint), whose
        # ``record_result`` does ``session.get(ExecutorTaskRow, task_id)`` — so
        # the task row MUST be visible (committed) to that session, or the worker
        # can never flip it terminal and ``await_completion`` blocks the full
        # timeout. Without this, the whole orchestrator transaction stayed open
        # across the (up to 30-min) external CLI run, hiding the row from every
        # other connection under Postgres READ COMMITTED. Committing here also
        # narrows the open-transaction window to the brief verified-write at the
        # end, and reflects reality: the run is genuinely RUNNING while the
        # external worker executes. (SQLite test runs share one StaticPool
        # connection so the hidden-row bug never surfaced there.)
        await self._session.commit()

        try:
            completed = await dispatch.await_completion(
                self._redis,
                session=self._session,
                task_id=task.id,
                timeout_s=self._settings.executor_task_timeout_s,
            )
        except TaskTimeout as exc:
            return await self._fail(
                run, work_step, attempt, summary=f"executor task timed out: {exc}"
            )

        if completed.status != "done":
            return await self._fail(
                run,
                work_step,
                attempt,
                summary=completed.error_message or "executor task failed",
            )

        # B2b — native verification convergence. The worker exited 0, but exit-0
        # is NOT proof: run the SAME verification the native loop runs against the
        # captured artifacts (B1 persisted them under the run dir) and set PROVED
        # only on a passing VerificationResult.
        return await self._verify_and_finish(
            run=run,
            work_step=work_step,
            attempt=attempt,
            task_id=task.id,
            artifact_refs=completed.artifact_refs or [],
            output=completed.output or "",
            workspace_dir=workspace_dir,
        )

    # -- verification convergence (B2b) ------------------------------------

    async def _verify_and_finish(
        self,
        *,
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
        await self._session.flush()

        # The sandbox is keyed on the run's project the same way native derives
        # it (``run.product_id or run.id``) and mounts the run dir B1 wrote into.
        project_id = run.product_id or run.id
        try:
            box = await self._sandbox_manager.acquire(project_id, str(workspace_dir))
        except Exception as exc:  # noqa: BLE001 — infra failure → system_error
            logger.warning(
                "executor_orchestrator_sandbox_unavailable", run_id=str(run.id), error=str(exc)
            )
            return await self._fail(run, work_step, attempt, summary=f"sandbox unavailable: {exc}")

        try:
            # The service requires a ``JudgeLlm``; when none is available we pass
            # a sentinel that raises if a judge call is ever attempted. It is
            # NEVER reached: a judge-bearing contract is routed to human review
            # below before ``verify`` runs, and command-only contracts make no
            # judge call (asserted by the VerificationService unit tests).
            judge: JudgeLlm = self._verify_llm or _UnavailableJudge()
            svc = VerificationService(session=self._session, llm=judge, retriever=self._retriever)
            contract = await svc.assemble_contract(
                declared_contract=None,
                written_paths=artifact_refs,
                final_text=output,
            )

            # HONEST branch 1 — no usable contract → human review (NOT a silent
            # pass, NOT PROVED). This is the anti-regression for the fake-PROVED
            # sin: exit-0 with nothing to check is NOT a verified deliverable.
            if contract is None:
                decision = await self._create_decision(
                    run,
                    kind=DECISION_HUMAN_REVIEW_REQUIRED,
                    rationale="executor produced work but there is no verifiable contract",
                    payload={"reason": "no_verifiable_contract", "artifact_refs": artifact_refs},
                )
                return self._decision_result(run, work_step, attempt, decision)

            # HONEST branch 2 — the contract has judge checks but no verify LLM is
            # available (e.g. executor-only-active workspace → no resolvable judge
            # account). Command-only contracts still run; a judge-bearing contract
            # we cannot grade routes to human review (NOT PROVED).
            if contract.judge_checks and self._verify_llm is None:
                decision = await self._create_decision(
                    run,
                    kind=DECISION_HUMAN_REVIEW_REQUIRED,
                    rationale="contract requires an LLM judge but no verification LLM is available",
                    payload={"reason": "no_verification_llm", "artifact_refs": artifact_refs},
                )
                return self._decision_result(run, work_step, attempt, decision)

            vr = await svc.verify(
                run=run,
                work_step=work_step,
                attempt=attempt,
                contract=contract,
                box=box,
                written_paths=artifact_refs,
                final_text=output,
            )
        except Exception as exc:  # noqa: BLE001 — any verify crash → system_error
            logger.exception("executor_orchestrator_verify_crash", run_id=str(run.id))
            return await self._fail(run, work_step, attempt, summary=f"verification crashed: {exc}")
        finally:
            try:
                await self._sandbox_manager.release(project_id)
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
            decision = await self._create_decision(
                run,
                kind=DECISION_VERIFICATION_FAILED,
                rationale="executor work failed the verification contract",
                payload={"artifact_refs": artifact_refs, "verification_result_id": str(vr.id)},
            )
            return self._decision_result(run, work_step, attempt, decision)

        # The ONLY PROVED path — gated on a real passing VerificationResult,
        # exactly like the native ``_finish_verified``.
        work_step.status = WorkStepStatus.VERIFIED
        work_step.proof_state = ProofState.PROVED
        attempt.phase = RunAttemptPhase.COMPLETED
        attempt.finished_at = _utcnow()
        deliverable = await write_verified_deliverable(
            self._session,
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
        return LoopResult(
            outcome="verified",
            run_id=run.id,
            work_step_id=work_step.id,
            run_attempt_id=attempt.id,
            verification_result_id=vr.id,
            written_paths=artifact_refs,
            summary=output,
        )

    # -- terminal helpers --------------------------------------------------

    async def _fail(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        *,
        summary: str,
    ) -> LoopResult:
        work_step.status = WorkStepStatus.FAILED
        attempt.phase = RunAttemptPhase.FAILED
        attempt.finished_at = _utcnow()
        await self._session.flush()
        logger.warning("executor_orchestrator_system_error", run_id=str(run.id), error=summary)
        return LoopResult(
            outcome="system_error",
            run_id=run.id,
            work_step_id=work_step.id,
            run_attempt_id=attempt.id,
            summary=summary,
        )

    async def _create_decision(
        self,
        run: ExecutionRun,
        *,
        kind: str,
        rationale: str,
        payload: dict[str, Any],
    ) -> Decision:
        decision = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            decision=kind,
            actor_id=None,
            rationale=rationale,
            payload=payload,
        )
        self._session.add(decision)
        await self._session.flush()
        logger.info("executor_orchestrator_needs_decision", run_id=str(run.id), kind=kind)
        return decision

    def _decision_result(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        decision: Decision,
    ) -> LoopResult:
        return LoopResult(
            outcome="needs_decision",
            run_id=run.id,
            work_step_id=work_step.id,
            run_attempt_id=attempt.id,
            decision_id=decision.id,
        )


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


def _parse_uuid(value: Any) -> uuid.UUID | None:
    """Best-effort parse of a stored ``worker_id`` tag (always a str in JSON)."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _utcnow() -> Any:
    from datetime import UTC, datetime  # noqa: PLC0415 — local to avoid top-level churn

    return datetime.now(tz=UTC)


__all__ = [
    "DECISION_HUMAN_REVIEW_REQUIRED",
    "DECISION_NO_DISPATCH_TRANSPORT",
    "DECISION_NO_WORKER_AVAILABLE",
    "DECISION_VERIFICATION_FAILED",
    "ExecutorOrchestrator",
]
