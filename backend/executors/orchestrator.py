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
5. On success → write the verified artifacts (shared helper); on failure /
   timeout → ``system_error``.

v1 TRUSTS the worker's success signal — there is no BSVibe-side re-verification
of the CLI output. Re-running the declared verification contract against the
worker's diff is a documented v2 refinement.
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
    WorkStep,
    WorkStepStatus,
)
from backend.execution.orchestrator import LoopResult
from backend.execution.verified_deliverable import write_verified_deliverable
from backend.executors import dispatch
from backend.executors.dispatch import TaskTimeout

logger = structlog.get_logger(__name__)

# Decision kinds raised when an executor run cannot dispatch.
DECISION_NO_WORKER_AVAILABLE = "no_executor_worker_available"
DECISION_NO_DISPATCH_TRANSPORT = "no_executor_dispatch_transport"


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
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._redis = redis
        self._account = account
        self._settings = settings or get_settings()

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
        task = await dispatch.create_task(
            self._session,
            workspace_id=run.workspace_id,
            executor_type=executor_type,
            prompt=prompt,
            workspace_dir=str(workspace_dir),
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

        # v1 trusts the worker's success signal — no BSVibe-side re-verification
        # of the CLI output (documented v2 refinement).
        work_step.status = WorkStepStatus.VERIFIED
        work_step.proof_state = ProofState.PROVED
        attempt.phase = RunAttemptPhase.COMPLETED
        attempt.finished_at = _utcnow()
        deliverable = await write_verified_deliverable(
            self._session,
            run,
            attempt_id=attempt.id,
            artifact_refs=[],
            summary=completed.output,
        )
        logger.info(
            "executor_orchestrator_verified",
            run_id=str(run.id),
            task_id=str(task.id),
            deliverable_id=str(deliverable.id),
        )
        return LoopResult(
            outcome="verified",
            run_id=run.id,
            work_step_id=work_step.id,
            run_attempt_id=attempt.id,
            written_paths=[],
            summary=completed.output,
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
    "DECISION_NO_DISPATCH_TRANSPORT",
    "DECISION_NO_WORKER_AVAILABLE",
    "ExecutorOrchestrator",
]
