"""RunOrchestrator — the agent compute loop.

Lifted from ``backend.execution.orchestrator`` (Lift H2a / v8 §17.1) —
the loop's conductor file. Holds the loop's I/O dataclasses + Protocols
(:class:`LoopTurn` / :class:`LoopToolCall` / :class:`LoopLlm` /
:class:`CanonRetriever` / :class:`LoopResult` / :class:`RunCompute`) and
the :class:`RunOrchestrator` class itself — the ``run`` entry point and
the ``_drive_loop`` plan→act→verify cycle.

The orchestrator's prompt + initial-context assembly lives in
:mod:`backend.workflow.application._loop_context`; tool-loop helpers in
:mod:`backend.workflow.application.tool_registry`; connector-action
surface in
:mod:`backend.workflow.application.connector_action_registrar`; the
DB-side helpers in
:mod:`backend.workflow.application.run_persistence`; mid-loop
deliverable emit in :mod:`backend.workflow.domain.emit_deliverable`.

The loop depends on ONE LLM seam — the :class:`LoopLlm` Protocol — so
the caller can inject the production gateway adapter
(:class:`~backend.workflow.application.loop_llm.ResolverLoopLlm`) or a deterministic
test stub. (Per the ``bsvibe-llm-wrapper-not-raw-litellm`` rule: one
Protocol, never a Union of concretes.)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.extensions.skill.loader import SkillLoader
from backend.knowledge.retrieval.knowledge_item import RetrievedKnowledge
from backend.workflow.application._loop_context import (
    _DESIGN_SPEC_DIRECTIVE,
    _SYSTEM_PROMPT,
    _intent_title,
    _is_design_stage,
    _resumption_messages,
    design_directive_message,
    design_seed_message,
    knowledge_seed_message,
    register_knowledge_tools,
    suggested_skill_message,
)
from backend.workflow.application.audit_events import (
    LoopTerminal,
    RunStarted,
)
from backend.workflow.application.connector_action_registrar import (
    register_connector_action_tools,
)
from backend.workflow.application.run_persistence import (
    audit_event,
    create_decision,
    decision_result,
    finish_verified,
    record_activity,
)
from backend.workflow.application.tool_registry import (
    ASK_USER_QUESTION_TOOL,
    WORK_TOOLS,
)
from backend.workflow.application.verification_service import VerificationService
from backend.workflow.domain.emit_deliverable import (
    EMIT_DELIVERABLE_NAME,
    EMIT_DELIVERABLE_TOOL,
)
from backend.workflow.domain.verifier_contract import VerificationContract
from backend.workflow.infrastructure.connector_actions import ConnectorActionProvider
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    ProofState,
    RunAttempt,
    RunAttemptPhase,
    RunStatus,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.workflow.infrastructure.sandbox import SandboxManager, SandboxSession
from backend.workflow.infrastructure.tools import ToolRegistry

logger = structlog.get_logger(__name__)

LoopOutcome = Literal["verified", "needs_decision", "system_error"]


@dataclass(frozen=True)
class LoopToolCall:
    """One tool call the work LLM emitted in a turn."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LoopTurn:
    """The work LLM's response for one plan turn — free text plus any
    tool calls. ``tool_calls`` empty means the model produced only prose
    (a signal that it believes the step is done)."""

    content: str
    tool_calls: tuple[LoopToolCall, ...] = ()
    # Files the compute backend captured for this turn OUTSIDE the loop's
    # file_write/file_edit tools. Coding-agent executors edit files in the
    # worker's per-task clone (captured worker-side as the task's
    # artifact_refs); those never flow through the loop's tool writes, so the
    # loop merges these into ``written_paths`` to record what actually changed.
    # Empty for the LiteLLM path (it writes via the loop's tools).
    artifact_refs: tuple[str, ...] = ()


@runtime_checkable
class LoopLlm(Protocol):
    """The single LLM dispatch seam the loop depends on.

    ``tools`` is the OpenAI-style tool schema slice; ``None`` means a
    plain completion (used for the LLM-judge verify call). Returns a
    :class:`LoopTurn`."""

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn: ...


@runtime_checkable
class CanonRetriever(Protocol):
    """Read-only BSage retrieval seam (Workflow §1.2). Given the signals
    of the change (changed paths + the work summary), returns canonical
    pattern statements to fold into the verify contract as judge criteria.

    Mirrors :class:`backend.workflow.application.verification_service.CanonRetriever`
    (this loop passes its retriever straight to ``VerificationService``):
    ``retrieve_structured`` carries each statement's identity for the report.
    """

    async def retrieve_for_signals(self, signals: str) -> list[str]: ...

    async def retrieve_structured(self, signals: str) -> list[RetrievedKnowledge]: ...


@dataclass
class LoopResult:
    """Outcome of one :meth:`RunOrchestrator.run` invocation."""

    outcome: LoopOutcome
    run_id: uuid.UUID
    work_step_id: uuid.UUID | None = None
    run_attempt_id: uuid.UUID | None = None
    verification_result_id: uuid.UUID | None = None
    decision_id: uuid.UUID | None = None
    written_paths: list[str] = field(default_factory=list)
    summary: str = ""


@runtime_checkable
class RunCompute(Protocol):
    """The single compute seam :class:`~backend.workflow.application.agent_runner.AgentRunner`
    drives.

    Both the native :class:`RunOrchestrator` (api-llm path) and the
    :class:`~backend.executors.orchestrator.ExecutorOrchestrator` (CLI-worker
    path, Lift 5b) satisfy it structurally, so the worker-runtime factory can
    return either without the runner depending on a Union of concretes (per the
    ``bsvibe-llm-wrapper-not-raw-litellm`` rule: one Protocol, never a Union).
    """

    async def run(self, *, run: ExecutionRun, workspace_dir: Path) -> LoopResult: ...


class RunOrchestrator:
    """Drives the plan → act → verify → iterate loop for one run."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        llm: LoopLlm,
        sandbox_manager: SandboxManager,
        max_cycles: int | None = None,
        retriever: CanonRetriever | None = None,
        skill_loader: SkillLoader | None = None,
        connector_actions: ConnectorActionProvider | None = None,
        redis_client: Any = None,
        settings: Settings | None = None,
        suggested_skill: str | None = None,
        suggested_skill_description: str | None = None,
        live_event_bus: Any = None,
    ) -> None:
        self._session = session
        self._llm = llm
        self._sandbox_manager = sandbox_manager
        self._settings = settings or get_settings()
        # B9a — the frame stage's matched skill (+ description), consumed as a
        # FIRST-INVOCATION hint: the loop's initial context nudges the work LLM
        # to invoke it via ``invoke_skill`` if appropriate. ``None`` (no frame
        # match / legacy caller) → no hint, loop unchanged.
        self._suggested_skill = suggested_skill
        self._suggested_skill_description = suggested_skill_description
        # B5b — connector-action provider. ``None`` (default) keeps the loop
        # free of connector tools. Lift 0c removed danger gating.
        self._connector_actions = connector_actions
        # B5a — workspace SkillLoader. ``None`` (default) keeps the original
        # WORK_TOOLS set.
        self._skill_loader = skill_loader
        self._max_cycles = (
            max_cycles if max_cycles is not None else self._settings.execution_work_round_budget
        )
        self._retriever = retriever
        # Only supplied in worker_mode="redis_streams"; ``None`` keeps the
        # DB-polling path. Emission is gated + soft-fail inside
        # ``emit_stream_notification``.
        self._redis_client = redis_client
        # D6 — optional LiveEventBus override for tests; production wires the
        # process-wide singleton via ``set_live_event_bus_redis``.
        self._live_event_bus = live_event_bus

    async def _skill_completion_fn(
        self,
        *,
        system_prompt: str,
        user_input: str,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
    ) -> str:
        """Adapt the skill runner's ``CompletionFn`` to the loop LLM seam.

        The skill runner expects ``(system_prompt, user_input, *, model,
        allowed_tools) -> str``; the loop LLM speaks ``(messages, tools) ->
        LoopTurn``. A skill is a single plain completion (no tool loop here), so
        we send the composed system prompt + user input as messages with
        ``tools=None`` and return the response text."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]
        turn = await self._llm.complete(messages=messages, tools=None)
        return turn.content

    # -- method delegations (signature stability for any caller) -----------

    def _register_knowledge_tools(self, registry: ToolRegistry) -> list[str]:
        return register_knowledge_tools(
            registry,
            skill_loader=self._skill_loader,
            retriever=self._retriever,
            completion_fn=self._skill_completion_fn,
        )

    async def _knowledge_seed_message(self, run: ExecutionRun) -> dict[str, Any] | None:
        return await knowledge_seed_message(run, retriever=self._retriever)

    def _design_directive_message(self, run: ExecutionRun) -> dict[str, Any] | None:
        return design_directive_message(run)

    def _design_seed_message(self, run: ExecutionRun) -> dict[str, Any] | None:
        return design_seed_message(run, settings=self._settings)

    def _suggested_skill_message(self) -> dict[str, Any] | None:
        return suggested_skill_message(
            suggested_skill=self._suggested_skill,
            suggested_skill_description=self._suggested_skill_description,
        )

    async def _knowledge_search(self, arguments: dict[str, Any]) -> str:
        from backend.workflow.application._loop_context import (  # noqa: PLC0415
            make_knowledge_search_handler,
        )

        result: str = await make_knowledge_search_handler(self._retriever)(arguments)
        return result

    async def _register_connector_action_tools(
        self, registry: ToolRegistry, *, run: ExecutionRun, work_step: WorkStep
    ) -> list[str]:
        return await register_connector_action_tools(
            registry,
            provider=self._connector_actions,
            run=run,
            work_step=work_step,
        )

    async def run(self, *, run: ExecutionRun, workspace_dir: Path) -> LoopResult:
        project_id = run.product_id or run.id
        work_step = WorkStep(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            title=_intent_title(run),
            status=WorkStepStatus.RUNNING,
            proof_state=ProofState.UNTESTED,
            payload={},
        )
        attempt = RunAttempt(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            phase=RunAttemptPhase.PLANNING,
            payload={},
        )
        self._session.add_all([work_step, attempt])
        await self._session.flush()

        # B15 — emit RunStarted onto the audit outbox the moment the run is
        # known to its WorkStep+RunAttempt rows. Soft-fail.
        await self._audit(run, attempt, RunStarted, {"intent": _intent_title(run)})

        try:
            box = await self._sandbox_manager.acquire(project_id, str(workspace_dir))
        except Exception as exc:  # noqa: BLE001 — infra failure → system_error
            await self._record(run, attempt, "error", {"stage": "acquire", "error": str(exc)})
            attempt.phase = RunAttemptPhase.FAILED
            work_step.status = WorkStepStatus.FAILED
            await self._session.flush()
            logger.warning(
                "run_orchestrator_sandbox_unavailable", run_id=str(run.id), error=str(exc)
            )
            await self._audit(
                run,
                attempt,
                LoopTerminal,
                {"outcome": "system_error", "stage": "acquire", "error": str(exc)},
            )
            return LoopResult(
                outcome="system_error",
                run_id=run.id,
                work_step_id=work_step.id,
                run_attempt_id=attempt.id,
                summary=f"sandbox unavailable: {exc}",
            )

        try:
            return await self._drive_loop(
                run=run,
                work_step=work_step,
                attempt=attempt,
                box=box,
                workspace_dir=workspace_dir,
            )
        except Exception as exc:  # noqa: BLE001 — any loop crash → system_error
            await self._record(run, attempt, "error", {"stage": "loop", "error": str(exc)})
            attempt.phase = RunAttemptPhase.FAILED
            work_step.status = WorkStepStatus.FAILED
            await self._session.flush()
            logger.exception("run_orchestrator_loop_crash", run_id=str(run.id))
            await self._audit(
                run,
                attempt,
                LoopTerminal,
                {"outcome": "system_error", "stage": "loop", "error": str(exc)},
            )
            return LoopResult(
                outcome="system_error",
                run_id=run.id,
                work_step_id=work_step.id,
                run_attempt_id=attempt.id,
                summary=f"loop crashed: {exc}",
            )
        finally:
            await self._sandbox_manager.release(project_id)

    async def _drive_loop(
        self,
        *,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        box: SandboxSession,
        workspace_dir: Path,
    ) -> LoopResult:
        from backend.workflow.application._drive_loop import (  # noqa: PLC0415
            drive_loop as _drive_loop_fn,
        )

        return await _drive_loop_fn(
            self,
            run=run,
            work_step=work_step,
            attempt=attempt,
            box=box,
            workspace_dir=workspace_dir,
        )

    # -- verify (delegated to the shared VerificationService) --------------

    def _verifier(self) -> VerificationService:
        """Build the shared verifier with this run's deps. The native loop and
        the executor orchestrator both go through this SAME service so they
        verify identically (Lift B2a)."""
        return VerificationService(session=self._session, llm=self._llm, retriever=self._retriever)

    async def _verify(
        self,
        *,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        contract: VerificationContract,
        box: SandboxSession,
        written_paths: list[str],
        final_text: str,
    ) -> VerificationResult:
        return await self._verifier().verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=written_paths,
            final_text=final_text,
        )

    async def _assemble_contract(
        self, registry: ToolRegistry, written_paths: list[str], final_text: str
    ) -> VerificationContract | None:
        return await self._verifier().assemble_contract(
            declared_contract=registry.declared_contract,
            written_paths=written_paths,
            final_text=final_text,
        )

    # -- run-persistence delegations (signature stability) -----------------

    async def _finish_verified(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        written_paths: list[str],
        final_text: str,
        verdict: VerificationResult,
    ) -> LoopResult:
        return await finish_verified(
            self._session,
            run=run,
            work_step=work_step,
            attempt=attempt,
            written_paths=written_paths,
            final_text=final_text,
            verdict=verdict,
            redis_client=self._redis_client,
            settings=self._settings,
        )

    async def _create_decision(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        *,
        kind: str,
        payload: dict[str, Any],
        rationale: str,
    ) -> Any:
        return await create_decision(
            self._session, run, work_step, kind=kind, payload=payload, rationale=rationale
        )

    def _decision_result(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        decision: Any,
        written_paths: list[str],
        final_text: str,
    ) -> LoopResult:
        return decision_result(run, work_step, attempt, decision, written_paths, final_text)

    async def _run_cancelled(self, run: ExecutionRun) -> bool:
        """Fresh-read the run status so a long in-flight attempt stops when the
        run is cancelled mid-loop.

        The loop's ``run`` ORM object was loaded when the attempt began; a
        cancel is written by a DIFFERENT session (founder action / operator),
        so we must re-read from the DB to see it. This is the cooperative side
        of cancel: the transition-time guard (``AgentRunner.transition`` no-ops
        a CANCELLED run) only fires at the terminal transition, so a multi-turn
        attempt kept dispatching turns to round-budget exhaustion after a cancel
        (dogfood dd2bd3a3). A column-level scalar select bypasses the identity
        map, returning the committed value under READ COMMITTED."""
        current = await self._session.scalar(
            select(ExecutionRun.status).where(ExecutionRun.id == run.id)
        )
        return current is RunStatus.CANCELLED

    def _cancelled_result(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        written_paths: list[str],
        final_text: str,
    ) -> LoopResult:
        """A benign terminal result for a run cancelled mid-loop. Mapped to NO
        status transition by :class:`AgentRunner` (``needs_decision`` is the
        non-transitioning outcome) — correct, because the run is already at the
        terminal CANCELLED state; the loop simply stops burning turns."""
        return LoopResult(
            outcome="needs_decision",
            run_id=run.id,
            work_step_id=work_step.id,
            run_attempt_id=attempt.id,
            written_paths=written_paths,
            summary="Run cancelled — agent loop stopped between turns.",
        )

    async def _record(
        self,
        run: ExecutionRun,
        attempt: RunAttempt,
        activity_type: str,
        payload: dict[str, Any],
    ) -> None:
        await record_activity(self._session, run, attempt, activity_type, payload)

    # -- B15: audit-stream emit (always soft-fail) -------------------------

    async def _audit(
        self,
        run: ExecutionRun,
        attempt: RunAttempt | None,
        event_cls: Any,
        data: dict[str, Any],
    ) -> None:
        await audit_event(self._session, run, attempt, event_cls, data)


__all__ = [
    "ASK_USER_QUESTION_TOOL",
    "EMIT_DELIVERABLE_NAME",
    "EMIT_DELIVERABLE_TOOL",
    "WORK_TOOLS",
    "CanonRetriever",
    "LoopLlm",
    "LoopOutcome",
    "LoopResult",
    "LoopToolCall",
    "LoopTurn",
    "RunCompute",
    "RunOrchestrator",
    "_DESIGN_SPEC_DIRECTIVE",
    "_SYSTEM_PROMPT",
    "_intent_title",
    "_is_design_stage",
    "_resumption_messages",
]
