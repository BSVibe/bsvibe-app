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
orchestrator — so :class:`~backend.workflow.application.agent_runner.AgentRunner.drive`
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

This file holds the ``ExecutorOrchestrator`` class + its dispatch flow.
The prompt assembly, terminal helpers, and verification convergence live in
sibling files (Lift D §17.8 4-file split):

* :mod:`backend.executors.prompt` — B8 context-rich prompt + system prompt.
* :mod:`backend.executors.terminal` — fail/decision/audit shared helpers.
* :mod:`backend.executors.verify_handoff` — B2b verification convergence.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.execution.audit_events import RunStarted
from backend.execution.db import (
    ExecutionRun,
    ProofState,
    RunAttempt,
    RunAttemptPhase,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.handoff import read_design_context
from backend.execution.orchestrator import LoopResult
from backend.execution.verifier.service import CanonRetriever, JudgeLlm
from backend.executors import dispatch
from backend.executors.dispatch import TaskTimeout
from backend.executors.prompt import (
    _assemble_executor_prompt,
    _executor_system_prompt,
    _intent_text,
    _parse_uuid,
)
from backend.executors.terminal import (
    create_decision,
    decision_terminal,
    emit_audit,
    fail_terminal,
)
from backend.executors.verify_handoff import verify_and_finish
from backend.router.accounts.models import ModelAccount
from backend.supervisor.sandbox import SandboxManager

logger = structlog.get_logger(__name__)

# Decision kinds raised when an executor run cannot dispatch.
DECISION_NO_WORKER_AVAILABLE = "no_executor_worker_available"
DECISION_NO_DISPATCH_TRANSPORT = "no_executor_dispatch_transport"


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

        # B15 — emit RunStarted onto the supervisor outbox the moment the
        # executor-driven run is known to its WorkStep+RunAttempt rows. The
        # ExecutorOrchestrator's compute backend is an external CLI worker,
        # but the audit-stream surface is unified across both orchestrators.
        await emit_audit(self._session, run, attempt, RunStarted, {"intent": _intent_text(run)})

        extra = self._account.extra_params or {}
        executor_type = str(extra.get("executor_type") or "")
        pinned_raw = extra.get("worker_id")
        pinned_worker_id = _parse_uuid(pinned_raw) if pinned_raw is not None else None

        # No Redis transport → no worker dispatch possible. Stuck → Decision
        # (never a silent stall): the worker daemon must run with a Redis client.
        if self._redis is None:
            decision = await create_decision(
                self._session,
                run,
                kind=DECISION_NO_DISPATCH_TRANSPORT,
                rationale="executor run has no Redis client to dispatch the worker task over",
                payload={"executor_type": executor_type},
            )
            return await decision_terminal(self._session, run, work_step, attempt, decision)

        worker = await dispatch.find_available_worker(
            self._session,
            workspace_id=run.workspace_id,
            executor_type=executor_type,
            pinned_worker_id=pinned_worker_id,
        )
        if worker is None:
            decision = await create_decision(
                self._session,
                run,
                kind=DECISION_NO_WORKER_AVAILABLE,
                rationale=f"no online worker with capability '{executor_type}'",
                payload={
                    "executor_type": executor_type,
                    "pinned_worker_id": str(pinned_worker_id) if pinned_worker_id else None,
                },
            )
            return await decision_terminal(self._session, run, work_step, attempt, decision)

        # B8 — assemble the context-rich CLI prompt (intent + relevant canon +
        # founder-resolved decisions) + a real engineer system prompt, instead of
        # the bare 512-char intent with an empty system. Graceful: a retriever
        # hiccup / no canon / no decisions degrades to intent-only — never raises
        # into dispatch.
        statements = await self._retrieve_canon(run)
        # P1-L2b — when this is the impl stage of a design→impl handoff, fold the
        # prior design stage's spec into the prompt so the CLI implements it.
        design_context = read_design_context(run, self._settings)
        prompt = _assemble_executor_prompt(
            run, statements=statements, design_context=design_context
        )
        system = _executor_system_prompt()
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
            system=system,
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
            return await fail_terminal(
                self._session,
                run,
                work_step,
                attempt,
                summary=f"executor task timed out: {exc}",
            )

        if completed.status != "done":
            return await fail_terminal(
                self._session,
                run,
                work_step,
                attempt,
                summary=completed.error_message or "executor task failed",
            )

        # B2b — native verification convergence. The worker exited 0, but exit-0
        # is NOT proof: run the SAME verification the native loop runs against the
        # captured artifacts (B1 persisted them under the run dir) and set PROVED
        # only on a passing VerificationResult.
        return await verify_and_finish(
            session=self._session,
            sandbox_manager=self._sandbox_manager,
            retriever=self._retriever,
            verify_llm=self._verify_llm,
            run=run,
            work_step=work_step,
            attempt=attempt,
            task_id=task.id,
            artifact_refs=completed.artifact_refs or [],
            output=completed.output or "",
            workspace_dir=workspace_dir,
        )

    # -- context assembly (B8) ---------------------------------------------

    async def _retrieve_canon(self, run: ExecutionRun) -> list[str]:
        """Retrieve canon relevant to the run's intent for the framed prompt (B8).

        Uses the SAME signal + retriever as the native B6 knowledge seed
        (``retrieve_for_signals(intent)``). No retriever → ``[]``; a retrieval
        hiccup degrades to ``[]`` (never raises into dispatch — exactly the
        graceful-empty contract the native seed/verify fold follow)."""
        if self._retriever is None:
            return []
        signals = _intent_text(run)
        try:
            statements = await self._retriever.retrieve_for_signals(signals)
        except Exception:  # noqa: BLE001 — canon priming must never crash dispatch
            logger.warning("executor_canon_retrieve_failed", run_id=str(run.id), exc_info=True)
            return []
        return list(statements)


__all__ = [
    "DECISION_NO_DISPATCH_TRANSPORT",
    "DECISION_NO_WORKER_AVAILABLE",
    "ExecutorOrchestrator",
]
