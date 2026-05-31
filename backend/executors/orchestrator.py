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

from backend.config import Settings, get_settings
from backend.execution.audit_events import (
    DecisionPending,
    LoopTerminal,
    RunStarted,
    VerifyRun,
)
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
from backend.execution.handoff import read_design_context
from backend.execution.orchestrator import LoopResult
from backend.execution.verified_deliverable import write_verified_deliverable
from backend.execution.verifier.service import CanonRetriever, JudgeLlm, VerificationService
from backend.executors import dispatch
from backend.executors.dispatch import TaskTimeout
from backend.router.accounts.models import ModelAccount
from backend.supervisor.audit.events import AuditActor, AuditEventBase, AuditResource
from backend.supervisor.audit.service import safe_emit
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


# B8 — context assembly for the CLI worker. Before B8 the executor shipped only
# a bare 512-char intent with an EMPTY system prompt — the CLI worked near-blind.
# B8 brings it to parity with the native loop: a real engineer system prompt
# (passed via ``create_task(system=...)``) + a context-rich framed prompt
# (intent + relevant canon + founder-resolved decisions). Caps respect the
# local-model generation budget (declared context window ≠ practical budget).

# The intent is now the REAL instruction (not a title), so the legacy 512-char
# cap is lifted to a few KB — still bounded so a runaway intent never blows the
# generation budget.
_INTENT_MAX_CHARS = 8_000
# Canon folded into the prompt as "Relevant established patterns" — top-N
# statements, each clamped (mirrors the native B6 knowledge seed: 5 × 500).
_KNOWLEDGE_MAX_RESULTS = 5
_KNOWLEDGE_MAX_CHARS_PER_STATEMENT = 500

# The executor system prompt — engineer guidance for a delegated CLI agent that
# runs its OWN tool loop (unlike the native loop, the CLI owns plan→act→verify).
# Adapted from the native ``_SYSTEM_PROMPT`` intent (do the framed work, produce
# the artifacts) but fitted to a self-driving CLI rather than the loop's
# declare_verification-gated tool protocol.
_EXECUTOR_SYSTEM_PROMPT = (
    "You are an autonomous software engineer executing a delegated task inside a "
    "working directory. Read the framed task, then use your own tools to inspect "
    "and change files until the work is complete. Produce the concrete "
    "artifacts the task asks for — write real files, run the relevant "
    "tests/lint, and leave the work in a verifiable state (your output is "
    "checked against a verification contract afterwards). Honor any established "
    "patterns and founder decisions included in the task. Do the work; do not "
    "ask for permission to proceed."
)

# D1b — when a run is the DESIGN stage of a ``design_then_impl`` pipeline, it
# must produce a SPECIFICATION (a concise markdown spec a later impl stage
# implements), NOT finished code. Today the design run gets the generic work
# prompt above with nothing telling it to spec rather than build, so it builds
# working code the impl stage then regenerates — a no-op merge (2026-05-28
# dogfood). This directive, prepended to the design run's work prompt, redirects
# it to spec. One concise instruction block (respect the local-model generation
# budget — not a heavy multi-section template). The ``single`` + ``impl`` work
# prompts never receive it (impl IMPLEMENTS the spec, so telling it to spec
# would reintroduce the no-op).
_DESIGN_SPEC_DIRECTIVE = (
    "THIS IS THE DESIGN STAGE. Write ONE concise markdown specification — do NOT "
    "implement it and do NOT write working code; a later implementation stage "
    "will. The spec MUST cover: Goal (what to build and why), "
    "Interface/Contract (the public API, signatures, inputs/outputs), File "
    "layout (the files to create and what each holds), and Acceptance criteria "
    "(observable conditions that prove the implementation is correct). Keep it "
    "tight and implementable; output only the spec."
)


def _intent_text(run: ExecutionRun, *, max_chars: int = 512) -> str:
    """The run's stable intent — the same input the native loop seeds with
    (``backend.execution.orchestrator._intent_title``).

    ``max_chars`` defaults to the legacy 512 cap used for the WorkStep title and
    canon-retrieval signal; the framed dispatch prompt lifts it to
    :data:`_INTENT_MAX_CHARS` (the intent is the real instruction there)."""
    payload = run.payload or {}
    text = payload.get("intent_text") or payload.get("text") or "Untitled run"
    return str(text)[:max_chars]


def _resolved_decisions(run: ExecutionRun) -> list[tuple[str, str]]:
    """Extract ``(question, answer)`` pairs from ``run.payload["resolved_decisions"]``.

    Same data the native ``_resumption_messages`` uses (appended by the
    checkpoints resolve endpoint). Entries without an answer / malformed entries
    are skipped. Always returns a list (never raises) — graceful for a resumed
    executor run with no decisions."""
    payload = run.payload or {}
    resolved = payload.get("resolved_decisions") if isinstance(payload, dict) else None
    if not isinstance(resolved, list):
        return []
    pairs: list[tuple[str, str]] = []
    for entry in resolved:
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("question") or "")
        answer = str(entry.get("answer") or "")
        if not answer:
            continue
        pairs.append((question, answer))
    return pairs


def _executor_system_prompt() -> str:
    """The engineer system prompt for the delegated CLI agent (B8)."""
    return _EXECUTOR_SYSTEM_PROMPT


def _is_design_stage(run: ExecutionRun) -> bool:
    """D1b — True when this run is the DESIGN stage of a ``design_then_impl``
    pipeline (so its work prompt is told to spec, not build).

    The condition mirrors routing's ``_derive_stage``: the FIRST run of a
    ``design_then_impl`` pipeline never carries an explicit ``stage`` (the
    AgentRunner chains impl off the frame's pipeline signal, not a stage
    column), so an unset / non-``impl`` stage on a ``design_then_impl`` run IS
    the design stage. The spawned implementation run carries ``stage="impl"`` and
    is excluded — it implements the spec. Any other pipeline (``single`` / no
    frame) is excluded. Tolerant of a missing/odd payload."""
    payload = run.payload if isinstance(run.payload, dict) else {}
    raw_frame = payload.get("frame")
    frame = raw_frame if isinstance(raw_frame, dict) else {}
    if frame.get("pipeline") != "design_then_impl":
        return False
    return payload.get("stage") != "impl"


def _assemble_executor_prompt(
    run: ExecutionRun, *, statements: list[str], design_context: str | None = None
) -> str:
    """Frame the context-rich CLI prompt: intent + canon + resolved decisions
    + (P1-L2b) the prior design stage's spec.

    Pure + synchronous (testable in isolation) — the caller does the async canon
    retrieval + design-spec read and passes the results. Sections that have no
    content are omitted entirely (no empty headers): an empty-knowledge,
    no-decisions run yields just the intent. Caps applied: the intent to
    :data:`_INTENT_MAX_CHARS`, canon to :data:`_KNOWLEDGE_MAX_RESULTS` × clamped
    statements (respect the local-model generation budget)."""
    parts: list[str] = [_intent_text(run, max_chars=_INTENT_MAX_CHARS)]

    # D1b — a DESIGN-stage run is told to write a spec, not build. Prepended
    # (after the intent) so it frames the whole task. Excludes single + impl.
    if _is_design_stage(run):
        parts.append(_DESIGN_SPEC_DIRECTIVE)

    if design_context:
        parts.append(design_context)

    cleaned = [
        s.strip()[:_KNOWLEDGE_MAX_CHARS_PER_STATEMENT] for s in statements if s and s.strip()
    ][:_KNOWLEDGE_MAX_RESULTS]
    if cleaned:
        body = "\n".join(f"- {s}" for s in cleaned)
        parts.append("Relevant established patterns for this workspace:\n" + body)

    decisions = _resolved_decisions(run)
    if decisions:
        lines = [f"- Q: {q} A: {a}" for q, a in decisions]
        parts.append(
            "The founder resolved these prior questions — honor them:\n" + "\n".join(lines)
        )

    return "\n\n".join(parts)


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
        await self._audit(run, attempt, RunStarted, {"intent": _intent_text(run)})

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
            return await self._decision_result(run, work_step, attempt, decision)

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
            return await self._decision_result(run, work_step, attempt, decision)

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
                return await self._decision_result(run, work_step, attempt, decision)

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
                return await self._decision_result(run, work_step, attempt, decision)

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
            await self._audit(
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
            return await self._decision_result(run, work_step, attempt, decision)

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
        # B15 — terminal (verified) — the founder-facing closing event.
        await self._audit(
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
        # B15 — terminal: system_error is the founder-facing closing event.
        await self._audit(
            run,
            attempt,
            LoopTerminal,
            {"outcome": "system_error", "summary": summary[:500]},
        )
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

    async def _decision_result(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        decision: Decision,
    ) -> LoopResult:
        # B15 — DecisionPending + the needs_decision terminal. Centralised here
        # so every executor decision path emits the same pair without each
        # caller remembering to. ``decision.payload`` carries the small reason
        # tag (no_executor_*/no_verifiable_contract/verification_failed/…).
        payload = decision.payload if isinstance(decision.payload, dict) else {}
        await self._audit(
            run,
            attempt,
            DecisionPending,
            {
                "kind": decision.decision,
                "decision_id": str(decision.id),
                "reason": payload.get("reason"),
            },
        )
        await self._audit(
            run,
            attempt,
            LoopTerminal,
            {"outcome": "needs_decision", "decision_id": str(decision.id)},
        )
        return LoopResult(
            outcome="needs_decision",
            run_id=run.id,
            work_step_id=work_step.id,
            run_attempt_id=attempt.id,
            decision_id=decision.id,
        )

    # -- B15: audit-stream emit (always soft-fail) -------------------------

    async def _audit(
        self,
        run: ExecutionRun,
        attempt: RunAttempt | None,
        event_cls: type[AuditEventBase],
        data: dict[str, Any],
    ) -> None:
        """Emit one audit event onto the supervisor outbox (B15).

        Mirrors :meth:`backend.execution.orchestrator.RunOrchestrator._audit`
        so the audit-stream surface is uniform across the two compute
        backends. Soft-fail via :func:`safe_emit`."""
        actor = AuditActor(type="system", id="backend.executors.executor_orchestrator")
        resource = AuditResource(type="execution_run", id=str(run.id))
        full_data: dict[str, Any] = {
            "run_id": str(run.id),
            "product_id": str(run.product_id) if run.product_id is not None else None,
        }
        if attempt is not None:
            full_data["attempt_id"] = str(attempt.id)
        full_data.update(data)
        event = event_cls(
            actor=actor,
            workspace_id=str(run.workspace_id),
            resource=resource,
            data=full_data,
        )
        await safe_emit(event, session=self._session)


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
