"""G6.3 / G6.6 — ``dispatch_run_attempt``: drive a Request's first
WorkStep through the RunAttempt phase machine.

G6.3 wired the single-shot path: one ``ExecutorClient.execute()``
call in ``work`` phase → persist a Deliverable → enqueue ``proof:queue``.

G6.6 promotes the ``work`` phase to a tool-call loop:

  1. Build the phase tool schema from :class:`ToolRegistry`.
  2. ``execute(messages, tools=schema)``.
  3. If the model returns ``tool_calls``, run each through the
     registry, record a ``ToolEvent`` via ``record_tool_event``
     (which enforces phase round budgets + repetition termination
     for free), append tool-result messages, loop.
  4. Otherwise (plain text response) — exit the loop, persist the
     last text as the deliverable summary, advance to verify / summarize.

Callers depend only on the :class:`ExecutorClient` Protocol so a third
executor kind stays one-class. A workspace_dir is required for the
tool loop; without one (legacy callers / G6.3 mode) the dispatcher
falls back to the single-shot path.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution._domain import (
    DeliverableType,
    ProofAspectStatus,
    ProofState,
    RunAttemptPhase,
    RunAttemptStatus,
    WorkStepStatus,
)
from backend.execution.deliverables import WorkOutputDraft, create_deliverable_from_work_output

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.core.executor_config.protocol
# from backend.src.core.executor_config.protocol import ExecutorClient
# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.core.executor_config.resolver
# from backend.src.core.executor_config.resolver import resolve_executor
from backend.execution.run_attempts import (
    ALLOWED_TOOLS_BY_PHASE,
    PHASE_ROUND_BUDGETS,
    ToolEventInput,
    accept_llm_phase_output,
    advance_phase,
    create_run_attempt,
    finish_run_attempt,
    record_tool_event,
)
from backend.execution.tools import ToolError, ToolRegistry
from backend.execution.verifier.judge import JudgeContext
from backend.execution.work_steps import transition_work_step
from backend.supervisor.sandbox import SandboxError, SandboxManager, SandboxSession
from backend.supervisor.sandbox.resolver import get_sandbox_manager

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.models
# from backend.src.models import Deliverable, Request, RunAttempt, WorkStep
# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.models.executor_config
# from backend.src.models.executor_config import ExecutorConfig
# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.queue.streams
# from backend.src.queue.streams import RedisStreamManager
# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.workers.verifier
# from backend.src.workers.verifier import PROOF_QUEUE_STREAM

logger = structlog.get_logger(__name__)


SUMMARY_PREVIEW_CHARS = 500

# G6.6 — hard cap on outer work-phase iterations. The
# ``record_tool_event`` per-phase budget already protects against
# runaway loops, but we also bound the *number of LLM round-trips* in
# case the model returns zero tool_calls but garbage text repeatedly
# (no ToolEvent rows means the round-budget logic never fires).
# Tier 1 (2026-05-16) raised this 64→80 to stay above the work-phase
# round budget (48) plus aspect-feedback retry headroom (≈20) — the
# outer cap must never bite before the phase budget, which is the
# signal the continuation system keys on.
MAX_WORK_LOOP_ITERATIONS = 80
MAX_NO_WORK_NUDGES = 2

# Aspect-feedback loop: after the model converges (natural exit, no
# tool_calls), the dispatcher probes the workspace's verification
# aspects (test / lint / install_smoke / build). If any blocking aspect
# fails, the failure summary is injected as a user message and the
# work phase re-enters for ``MAX_ASPECT_RETRY_ROUNDS`` more turns so
# the model can self-correct from the actual verifier output instead
# of guessing from prompt rules. Retries are capped so a stubborn
# failure doesn't pin the model in an infinite loop.
MAX_ASPECT_RETRIES = 2
MAX_ASPECT_RETRY_ROUNDS = 10
# Work-rounds that must remain in the budget for another retry to be
# worth starting. Below this floor the retry would be cut short by the
# budget anyway — give up and route through the Tier 1 handoff /
# escalation path (a fresh continuation gets a full budget; if at the
# continuation cap it escalates to a founder Decision). Prevents the
# loop from burning the last few rounds on a doomed partial retry.
ASPECT_RETRY_BUDGET_FLOOR = 12

# Tier 1 continuation system. When a RunAttempt exhausts its work-round
# budget but made real progress (file writes landed), it is NOT a
# failure — it is a checkpoint. A model-independent handoff record is
# generated and a fresh RunAttempt for the SAME WorkStep resumes the
# work. Bounded so genuinely-pathological work still terminates:
# original attempt + up to MAX_BUDGET_CONTINUATIONS continuations.
# See ~/Docs/BSNexus_Budget_Handoff_Continuation_Design_2026-05-16.md.
MAX_BUDGET_CONTINUATIONS = 3
# Rounds before the work-phase budget at which a soft "wrap up the
# change you're on, then stop" message is injected so the model lands
# in a clean state rather than being hard-cut mid-edit.
SOFT_PRESSURE_HEADROOM = 4
# Terminal reasons that mean "the attempt ran out of room" — every one
# of these routes through the Tier 1 continuation / Decision path
# rather than dead-ending. Continuation no longer requires file writes:
# a 0-write exhaustion (``failed_nonconvergent:no_workspace_write``) is
# just as eligible — a fresh attempt gets a clean budget, and at the
# continuation cap a founder Decision is raised. There is no longer a
# silent ``blocked`` dead-end.
_BUDGET_TERMINATION_REASONS = frozenset(
    {
        "phase_round_budget_exceeded:work",
        "work_loop_iteration_cap",
        "catastrophic_round_budget_exceeded",
        # The aspect-feedback loop gave up because too little work-round
        # budget remained for another retry — a budget-class outcome, so
        # it routes through the same handoff / continuation path.
        "aspect_retry_budget_exhausted",
        # The model produced no file write within its nudge budget. A
        # fresh continuation gets a clean round budget; at the cap this
        # escalates to a founder Decision instead of a dead-end.
        "failed_nonconvergent:no_workspace_write",
    }
)


def _is_budget_termination(reason: str) -> bool:
    """True when ``reason`` is a budget-class termination — the model
    ran out of rounds, as opposed to a genuine stall (0 writes, repeated
    tool call, tool error)."""
    return reason in _BUDGET_TERMINATION_REASONS


@dataclass(frozen=True)
class DispatchRunAttemptResult:
    """Outcome of one ``dispatch_run_attempt`` call.

    ``deliverable`` is ``None`` when the dispatch failed before any
    output could be persisted (no executor config / executor error);
    ``terminal_reason`` mirrors ``RunAttempt.terminal_reason`` so the
    caller doesn't have to refresh the row.
    """

    attempt: RunAttempt
    deliverable: Deliverable | None
    terminal_reason: str


@dataclass
class _AttemptOutcome:
    """Outcome of one RunAttempt inside ``dispatch_run_attempt``'s
    continuation loop. Exactly one of the two fields is set:

    - ``result`` — a final ``DispatchRunAttemptResult``; the WorkStep
      is in a terminal state, stop.
    - ``handoff`` — the RunAttempt exhausted its budget with real
      progress; this is the model-independent handoff record for the
      next continuation RunAttempt to resume from.
    """

    result: DispatchRunAttemptResult | None = None
    handoff: dict[str, Any] | None = None


async def dispatch_run_attempt(
    *,
    request: Request,
    work_step: WorkStep,
    tenant_id: uuid.UUID,
    session: AsyncSession,
    stream_manager: RedisStreamManager,
    executor: ExecutorClient | None = None,
    executor_kind: str | None = None,
    model: str | None = None,
    workspace_dir: Path | str | None = None,
    step_index: int | None = None,
    total_steps: int | None = None,
    prior_step_names: tuple[str, ...] = (),
    sandbox_manager: SandboxManager | None = None,
    seed_handoff: dict[str, Any] | None = None,
) -> DispatchRunAttemptResult:
    """Drive ``work_step`` to a terminal state and enqueue the
    resulting Deliverable on ``proof:queue``.

    Tier 1 continuation: a RunAttempt that exhausts its work-round
    budget with real progress is not a failure — a handoff record is
    generated and a fresh RunAttempt for the SAME WorkStep resumes the
    work (bounded by ``MAX_BUDGET_CONTINUATIONS``). The WorkStep stays
    ``running`` across the chain; the founder never sees a budget cut.

    Failure modes never raise — they're encoded as
    ``RunAttemptStatus.failed`` with a stable ``terminal_reason``
    string so the M0 harness can bucket by reason without try/except
    plumbing per call site.
    """
    if executor is None:
        kind, resolved_model = await _lookup_executor_config_kind_and_model(
            tenant_id=tenant_id, session=session
        )
        if kind is None:
            return await _finish_unconfigured(work_step=work_step, session=session)
        executor = await resolve_executor(tenant_id=tenant_id, session=session)
        if executor is None:
            return await _finish_unconfigured(work_step=work_step, session=session)
        executor_kind = kind
        model = resolved_model

    if executor_kind is None:
        executor_kind = "injected"

    # Part B — acquire the project's sandbox session once; it is reused
    # across the whole continuation chain and aspect-retry rounds (the
    # sandbox is per-project, not per-RunAttempt). With sandbox_enabled
    # false the resolver returns None → no session → the host path runs
    # unchanged. A sandbox backend that is unreachable degrades to the
    # host path rather than failing the work step.
    if sandbox_manager is None:
        sandbox_manager = get_sandbox_manager()
    sandbox_session: SandboxSession | None = None
    if sandbox_manager is not None and workspace_dir is not None:
        try:
            sandbox_session = await sandbox_manager.acquire(request.project_id, str(workspace_dir))
        except SandboxError:
            logger.exception(
                "sandbox_acquire_failed_degrading_to_host",
                request_id=str(request.id),
                project_id=str(request.project_id),
            )
            sandbox_session = None

    # ``seed_handoff`` from the caller (a ``reframe`` re-engagement after
    # a founder Decision) seeds the FIRST attempt with founder guidance;
    # the continuation loop then overwrites it with generated handoffs.
    outcome = _AttemptOutcome()
    for continuation_index in range(MAX_BUDGET_CONTINUATIONS + 1):
        outcome = await _execute_one_attempt(
            request=request,
            work_step=work_step,
            tenant_id=tenant_id,
            session=session,
            stream_manager=stream_manager,
            executor=executor,
            executor_kind=executor_kind,
            model=model,
            workspace_dir=workspace_dir,
            step_index=step_index,
            total_steps=total_steps,
            prior_step_names=prior_step_names,
            seed_handoff=seed_handoff,
            can_continue=continuation_index < MAX_BUDGET_CONTINUATIONS,
            sandbox_session=sandbox_session,
        )
        if outcome.handoff is None:
            return outcome.result  # type: ignore[return-value]
        seed_handoff = outcome.handoff
        logger.info(
            "run_attempt_continuation",
            work_step_id=str(work_step.id),
            continuation=continuation_index + 1,
        )
    # The final loop pass ran with can_continue=False, so its outcome
    # always carries a final result — never a handoff.
    return outcome.result  # type: ignore[return-value]


def _capture_verification_contract(attempt: RunAttempt, tool_registry: ToolRegistry | None) -> None:
    """Persist the contract the work LLM declared via the
    ``declare_verification`` tool onto the RunAttempt. No-op when the
    model never declared one — that case falls to ``human_review_
    required`` at verification time (a TDD-less step needs review)."""
    if tool_registry is not None and tool_registry.declared_contract is not None:
        attempt.verification_contract = tool_registry.declared_contract


async def _execute_one_attempt(
    *,
    request: Request,
    work_step: WorkStep,
    tenant_id: uuid.UUID,
    session: AsyncSession,
    stream_manager: RedisStreamManager,
    executor: ExecutorClient,
    executor_kind: str,
    model: str | None,
    workspace_dir: Path | str | None,
    step_index: int | None,
    total_steps: int | None,
    prior_step_names: tuple[str, ...],
    seed_handoff: dict[str, Any] | None,
    can_continue: bool,
    sandbox_session: SandboxSession | None = None,
) -> _AttemptOutcome:
    """Run one RunAttempt for ``work_step``. Returns an ``_AttemptOutcome``
    — a final result, OR a handoff record signalling continuation when
    the budget was exhausted with real progress and ``can_continue``."""
    attempt = await create_run_attempt(
        work_step=work_step,
        executor_kind=executor_kind,
        model=model,
        session=session,
    )
    # A continuation RunAttempt finds the WorkStep already ``running`` —
    # it stays ``running`` across the whole RunAttempt chain.
    if work_step.status != WorkStepStatus.running:
        await transition_work_step(step=work_step, target=WorkStepStatus.running, session=session)
    await advance_phase(attempt=attempt, target=RunAttemptPhase.work, session=session)

    metadata = {
        "tenant_id": str(tenant_id),
        "run_id": str(attempt.id),
        "request_id": str(request.id),
        "project_id": str(request.project_id),
    }
    tool_registry = _build_tool_registry(workspace_dir, sandbox_session)
    workspace_overview = _workspace_overview(workspace_dir) if tool_registry is not None else None
    agents_md = _read_agents_md(workspace_dir)
    messages = _build_messages(
        request=request,
        work_step=work_step,
        workspace_overview=workspace_overview,
        step_index=step_index,
        total_steps=total_steps,
        prior_step_names=prior_step_names,
        agents_md=agents_md,
        handoff=seed_handoff,
    )

    try:
        output_text, written_paths = await _run_work_phase(
            attempt=attempt,
            messages=messages,
            metadata=metadata,
            model=model or "",
            executor=executor,
            tool_registry=tool_registry,
            workspace_dir=workspace_dir,
            session=session,
        )
        # Aspect-feedback retry loop. Probe the workspace's verification
        # aspects; if any blocking aspect failed, inject the failure
        # output as a user message and re-enter the work phase for up
        # to MAX_ASPECT_RETRY_ROUNDS more rounds. The model now self-
        # corrects from the actual verifier output instead of needing
        # an exhaustive list of "trap X causes Y" rules in the prompt.
        # Skipped when the work phase produced no writes (nothing to
        # verify) or the workspace dir isn't set (no aspects apply).
        if written_paths and workspace_dir is not None:
            output_text, written_paths = await _aspect_feedback_retry_loop(
                attempt=attempt,
                request=request,
                workspace_dir=workspace_dir,
                output_text=output_text,
                written_paths=written_paths,
                messages=messages,
                metadata=metadata,
                model=model or "",
                executor=executor,
                tool_registry=tool_registry,
                session=session,
                sandbox_session=sandbox_session,
            )
        _capture_verification_contract(attempt, tool_registry)
    except _ToolLoopTerminated as terminated:
        _capture_verification_contract(terminated.attempt, tool_registry)
        is_exhaustion = _is_budget_termination(terminated.reason)
        # Budget / round / no-write exhaustion + room left → checkpoint,
        # not failure: generate a model-independent handoff record and
        # signal the continuation loop. The WorkStep stays ``running`` —
        # a fresh RunAttempt resumes with a clean budget. This fires
        # regardless of ``written_paths``: a 0-write stall is just as
        # eligible — the handoff carries an empty file list and the
        # continuation prompt tells the next attempt to start the work.
        if can_continue and is_exhaustion:
            handoff = await _generate_handoff_record(
                attempt=terminated.attempt,
                work_step=work_step,
                messages=messages,
                written_paths=terminated.written_paths,
                final_text=terminated.final_text,
                model=model or "",
                executor=executor,
                metadata=metadata,
                workspace_dir=workspace_dir,
                session=session,
            )
            return _AttemptOutcome(handoff=handoff)

        # Exhaustion with the continuation cap reached → this is NOT a
        # dead-end. Park the WorkStep at ``needs_decision`` and raise a
        # blocking founder Decision; resolving it re-dispatches the work.
        if is_exhaustion:
            await transition_work_step(
                step=work_step, target=WorkStepStatus.needs_decision, session=session
            )
            # Lazy-import to break the orchestration ↔ run_attempt_executor
            # import cycle (orchestration imports dispatch_run_attempt).
            from backend.execution.orchestrator import create_blocking_decision  # noqa: PLC0415

            await create_blocking_decision(
                request=request,
                work_step=work_step,
                reason=terminated.reason,
                session=session,
                stream_manager=stream_manager,
            )
            partial_deliverable = await _surface_partial_deliverable(
                terminated=terminated,
                request=request,
                work_step=work_step,
                tenant_id=tenant_id,
                workspace_dir=workspace_dir,
                executor=executor,
                model=model,
                metadata=metadata,
                sandbox_session=sandbox_session,
                session=session,
            )
            await session.commit()
            return _AttemptOutcome(
                result=DispatchRunAttemptResult(
                    attempt=terminated.attempt,
                    deliverable=partial_deliverable,
                    terminal_reason=terminated.reason,
                )
            )

        await transition_work_step(step=work_step, target=WorkStepStatus.failed, session=session)
        # Defensive: every non-budget terminator should have finished its
        # RunAttempt at the raise site, but guard against a leaked
        # ``running`` attempt here too — the one place all
        # ``_ToolLoopTerminated`` flows converge — so a forgotten
        # raise-site never leaves a zombie row behind a failed WorkStep.
        if terminated.attempt.status == RunAttemptStatus.running:
            await finish_run_attempt(
                attempt=terminated.attempt,
                status=RunAttemptStatus.failed,
                terminal_reason=terminated.reason,
                session=session,
            )
        partial_deliverable = await _surface_partial_deliverable(
            terminated=terminated,
            request=request,
            work_step=work_step,
            tenant_id=tenant_id,
            workspace_dir=workspace_dir,
            executor=executor,
            model=model,
            metadata=metadata,
            sandbox_session=sandbox_session,
            session=session,
        )
        return _AttemptOutcome(
            result=DispatchRunAttemptResult(
                attempt=terminated.attempt,
                deliverable=partial_deliverable,
                terminal_reason=terminated.reason,
            )
        )
    except Exception as exc:
        reason = f"executor_error:{exc.__class__.__name__}"
        logger.warning(
            "dispatch_run_attempt_executor_error",
            tenant_id=str(tenant_id),
            request_id=str(request.id),
            work_step_id=str(work_step.id),
            run_attempt_id=str(attempt.id),
            error=str(exc),
        )
        await finish_run_attempt(
            attempt=attempt,
            status=RunAttemptStatus.failed,
            terminal_reason=reason,
            session=session,
        )
        await transition_work_step(step=work_step, target=WorkStepStatus.failed, session=session)
        return _AttemptOutcome(
            result=DispatchRunAttemptResult(
                attempt=attempt, deliverable=None, terminal_reason=reason
            )
        )

    await advance_phase(attempt=attempt, target=RunAttemptPhase.verify, session=session)
    await advance_phase(attempt=attempt, target=RunAttemptPhase.summarize, session=session)
    accept_llm_phase_output(
        attempt=attempt,
        payload={"summary": output_text[:SUMMARY_PREVIEW_CHARS]},
    )

    await finish_run_attempt(
        attempt=attempt,
        status=RunAttemptStatus.completed,
        terminal_reason="summarized",
        session=session,
    )

    deliverable = await create_deliverable_from_work_output(
        tenant_id=tenant_id,
        draft=WorkOutputDraft(
            project_id=request.project_id,
            request_id=request.id,
            work_step_id=work_step.id,
            title=work_step.name,
            summary=output_text[:SUMMARY_PREVIEW_CHARS] or None,
            type=DeliverableType.code,
            artifact_refs=written_paths,
        ),
        session=session,
    )

    await transition_work_step(step=work_step, target=WorkStepStatus.verifying, session=session)

    await stream_manager.publish(
        PROOF_QUEUE_STREAM,
        {
            "deliverable_id": str(deliverable.id),
            "tenant_id": str(tenant_id),
        },
    )

    return _AttemptOutcome(
        result=DispatchRunAttemptResult(
            attempt=attempt,
            deliverable=deliverable,
            terminal_reason="summarized",
        )
    )


async def _surface_partial_deliverable(
    *,
    terminated: _ToolLoopTerminated,
    request: Request,
    work_step: WorkStep,
    tenant_id: uuid.UUID,
    workspace_dir: Path | str | None,
    executor: ExecutorClient,
    model: str | None,
    metadata: dict[str, Any],
    sandbox_session: SandboxSession | None,
    session: AsyncSession,
) -> Deliverable | None:
    """Partial-work preservation for a terminated RunAttempt.

    If the model produced any file writes before termination, surface
    them as a deliverable so the founder can inspect what landed instead
    of staring at a null deliverable. Aspects run for *diagnostic*
    signal (which lint errors? which tests fail?) — but a non-converged
    run can never be auto-``verified``, so the roll-up is capped at
    ``human_review_required``. No ``proof:queue`` enqueue — the verifier
    worker must not overwrite the state the caller just stamped.

    Returns ``None`` when nothing was written.
    """
    if not terminated.written_paths:
        return None
    partial_deliverable = await create_deliverable_from_work_output(
        tenant_id=tenant_id,
        draft=WorkOutputDraft(
            project_id=request.project_id,
            request_id=request.id,
            work_step_id=work_step.id,
            title=work_step.name,
            summary=(terminated.final_text or "")[:SUMMARY_PREVIEW_CHARS] or None,
            type=DeliverableType.code,
            artifact_refs=list(terminated.written_paths),
        ),
        session=session,
    )
    await session.flush()
    # Lazy-import: ``orchestration`` → ``run_attempt_executor`` →
    # ``verification`` shares the import-cycle break set up for the
    # back-half orchestration hook.
    from backend.execution.verification import run_verification  # noqa: PLC0415

    try:
        await run_verification(
            deliverable=partial_deliverable,
            workspace_root=workspace_dir or "/tmp",
            session=session,
            changed_files=tuple(terminated.written_paths),
            verification_contract=terminated.attempt.verification_contract,
            judge=JudgeContext(executor=executor, model=model or "", metadata=metadata),
            sandbox_session=sandbox_session,
        )
    except Exception:
        logger.exception(
            "partial_deliverable_verification_crashed",
            deliverable_id=str(partial_deliverable.id),
        )
    # Demote ``verified`` → ``human_review_required``. The aspects ran
    # for diagnostics, but the model didn't reach a final summary; the
    # run is non-converged by definition.
    if partial_deliverable.proof_state == ProofState.verified:
        partial_deliverable.proof_state = ProofState.human_review_required
        await session.flush()
    return partial_deliverable


async def _lookup_executor_config_kind_and_model(
    *, tenant_id: uuid.UUID, session: AsyncSession
) -> tuple[str | None, str | None]:
    """Return ``(kind, model)`` from the per-tenant
    :class:`ExecutorConfig` row, or ``(None, None)`` when no row
    exists. Pulled out so tests can patch it without standing up the
    encryption manager.
    """
    config = (
        await session.execute(select(ExecutorConfig).where(ExecutorConfig.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if config is None:
        return None, None
    return config.kind.value, config.model


async def _finish_unconfigured(
    *, work_step: WorkStep, session: AsyncSession
) -> DispatchRunAttemptResult:
    """Record a failed RunAttempt + WorkStep transition for the
    "no per-tenant executor config" case so the audit trail shows
    *why* nothing dispatched."""
    attempt = await create_run_attempt(
        work_step=work_step,
        executor_kind="unconfigured",
        model=None,
        session=session,
    )
    await transition_work_step(step=work_step, target=WorkStepStatus.running, session=session)
    await finish_run_attempt(
        attempt=attempt,
        status=RunAttemptStatus.failed,
        terminal_reason="executor_unconfigured",
        session=session,
    )
    await transition_work_step(step=work_step, target=WorkStepStatus.failed, session=session)
    return DispatchRunAttemptResult(
        attempt=attempt,
        deliverable=None,
        terminal_reason="executor_unconfigured",
    )


_HANDOFF_FIELDS = ("summary", "files_touched", "verification_state", "remaining", "blockers")


def _fallback_handoff(written_paths: list[str], final_text: str) -> dict[str, Any]:
    """Minimal handoff record used when the generation LLM call fails
    or returns unparseable output. The workspace files are the real
    state; the record is only an aid (design Q9)."""
    return {
        "summary": (final_text or "Previous attempt ran out of round budget.")[
            :SUMMARY_PREVIEW_CHARS
        ],
        "files_touched": list(written_paths),
        "verification_state": "unknown — handoff generation unavailable",
        "remaining": "Inspect the workspace files for current state, then finish the work step objective.",
        "blockers": "",
    }


def _coerce_handoff(raw: Any, written_paths: list[str], final_text: str) -> dict[str, Any]:
    """Normalize an LLM-produced handoff value into the fixed schema."""
    if not isinstance(raw, dict):
        return _fallback_handoff(written_paths, final_text)
    files = raw.get("files_touched")
    if not isinstance(files, list):
        files = list(written_paths)
    else:
        files = [str(f).strip() for f in files if str(f).strip()] or list(written_paths)
    return {
        "summary": str(raw.get("summary") or "").strip()
        or _fallback_handoff(written_paths, final_text)["summary"],
        "files_touched": files,
        "verification_state": str(raw.get("verification_state") or "").strip() or "(unknown)",
        "remaining": str(raw.get("remaining") or "").strip() or "Finish the work step objective.",
        "blockers": str(raw.get("blockers") or "").strip(),
    }


async def _generate_handoff_record(
    *,
    attempt: RunAttempt,
    work_step: WorkStep,
    messages: list[dict[str, Any]],
    written_paths: list[str],
    final_text: str,
    model: str,
    executor: ExecutorClient,
    metadata: dict[str, Any],
    workspace_dir: Path | str | None,
    session: AsyncSession,
) -> dict[str, Any]:
    """Generate a model-independent handoff record for the next
    continuation RunAttempt and persist it on ``attempt.handoff``.

    One LLM call (no tools) over the work conversation. On any failure
    a minimal fallback record is used — the continuation still proceeds
    because the workspace files are the real state."""
    handoff_messages = [
        *messages,
        {
            "role": "user",
            "content": (
                "This work step's attempt has run out of round budget. A FRESH attempt will "
                "continue it — that attempt will NOT see this conversation, only the workspace "
                "files on disk plus the handoff record you produce now. Output ONLY a JSON "
                "object, no prose, with exactly these fields:\n"
                '{"summary": "one paragraph — what you accomplished", '
                '"files_touched": ["paths you created or modified"], '
                '"verification_state": "what passes and what still fails", '
                '"remaining": "concrete next actions to finish the objective", '
                '"blockers": "known issues, or empty string"}\n'
                "Be concrete — the next attempt depends entirely on this record plus the "
                "workspace files."
            ),
        },
    ]
    handoff: dict[str, Any]
    try:
        result = await executor.execute(
            messages=handoff_messages,
            metadata=metadata,
            model=model,
            workspace_dir=str(workspace_dir) if workspace_dir is not None else None,
            tools=None,
        )
        text = str(result.get("output_ref") or "")
        start, end = text.find("{"), text.rfind("}")
        raw = json.loads(text[start : end + 1]) if 0 <= start < end else None
        handoff = _coerce_handoff(raw, written_paths, final_text)
    except Exception as exc:  # noqa: BLE001 — handoff generation must never bubble
        logger.warning(
            "handoff_generation_failed",
            run_attempt_id=str(attempt.id),
            work_step_id=str(work_step.id),
            error=str(exc),
        )
        handoff = _fallback_handoff(written_paths, final_text)

    attempt.handoff = handoff
    await session.commit()
    logger.info(
        "handoff_record_generated",
        run_attempt_id=str(attempt.id),
        work_step_id=str(work_step.id),
        files=len(handoff["files_touched"]),
    )
    return handoff


def _build_messages(
    *,
    request: Request,
    work_step: WorkStep,
    workspace_overview: str | None = None,
    step_index: int | None = None,
    total_steps: int | None = None,
    prior_step_names: tuple[str, ...] = (),
    agents_md: str | None = None,
    handoff: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    expected = "\n".join(f"- {item}" for item in (work_step.expected_outputs or []))
    user_block = f"Request intent:\n{request.intent}\n\n"
    # Tier 1 continuation — this RunAttempt resumes a predecessor that
    # ran out of budget. The handoff record is model-independent data
    # (no inherited LLM message history); the workspace already holds
    # the prior progress on disk.
    if handoff:
        user_block += (
            "CONTINUATION — a previous attempt at this work step ran out of "
            "round budget. Resume from its handoff; the workspace already "
            "contains the prior progress on disk — read it, do NOT recreate "
            "it.\n"
            f"  Progress so far: {str(handoff.get('summary') or '').strip()}\n"
            f"  Files already touched: {', '.join(handoff.get('files_touched') or []) or '(none recorded)'}\n"
            f"  Verification state: {str(handoff.get('verification_state') or '').strip() or '(unknown)'}\n"
            f"  Remaining work: {str(handoff.get('remaining') or '').strip() or '(finish the objective below)'}\n"
            f"  Known blockers: {str(handoff.get('blockers') or '').strip() or '(none recorded)'}\n\n"
        )
    # Auxiliary, founder-editable conventions. The system prompt above
    # is the universal base; AGENTS.md is per-project guidance the
    # founder owns. Injected into the user block (not the system
    # prompt) so it reads as project context, not framework law.
    if agents_md:
        user_block += (
            "Project conventions (from AGENTS.md — follow these unless they "
            f"conflict with a hard verifier requirement):\n{agents_md.strip()}\n\n"
        )
    # G10 — when the WorkPlan has multiple steps, tell the model where
    # it sits in the sequence. Keeps each step focused on its own
    # objective instead of trying to satisfy the whole Request in one
    # shot. Single-step plans get the original user block.
    if step_index is not None and total_steps is not None and total_steps > 1:
        user_block += f"This is step {step_index + 1} of {total_steps}.\n"
        if prior_step_names:
            user_block += "Already completed steps: " + ", ".join(prior_step_names) + ".\n"
        user_block += (
            "Focus only on this step's objective and expected_outputs. "
            "Earlier steps' work is already on disk — read it if helpful, "
            "but do not redo it.\n\n"
        )
    user_block += f"Work step: {work_step.name}\nObjective: {work_step.objective}\n"
    if expected:
        user_block += f"Expected outputs:\n{expected}\n"
    if workspace_overview:
        user_block += f"\nWorkspace contents (top-level):\n{workspace_overview}\n"
    return [
        {
            "role": "system",
            "content": (
                "You execute a single coding work step for an AI company. The user can only "
                "verify your work via files on disk and the project's verifier (its test and "
                "lint commands). RULES:\n"
                "1. You MUST use file_write at least once to create or modify a code file. "
                "Reading and listing alone are not a deliverable.\n"
                "2. Plan briefly, then act: pick a target path, write the file, run the verifier "
                "via shell_exec, then send a one-paragraph plain-text summary (no tool calls) to "
                "hand off.\n"
                "3. Do not respond with prose explaining what you would do — do it with tools first.\n"
                "4. If the workspace is empty or sparse, that's expected; create the files you need.\n"
                "5. Stay inside the workspace. Path traversal and destructive shell commands are "
                "blocked at the tool boundary.\n"
                "6. PRESERVE existing tests and code. If a file already exists in the workspace, "
                "file_read it first, then change it with file_edit — a surgical exact-string "
                "replacement. Do NOT use file_write to modify an existing file: file_write "
                "replaces the whole file, and rewriting a large file from memory silently drops "
                "or corrupts the parts you did not mean to touch. file_write is for NEW files "
                "only; file_edit is for changing existing ones. Do not delete tests that cover "
                "behaviour outside this work step's scope. Overwriting prior work breaks the "
                "cumulative loop.\n"
                "7. FIX FAILING TESTS BEFORE ADDING NEW CODE. If the verifier reports a failure, "
                "read it, fix the code OR the test, and re-run BEFORE moving on. The verifier "
                "blocks the deliverable on a single failure regardless of how much else is added.\n"
                "8. ENVIRONMENT IS REPO-DEFINED. The sandbox carries a generic toolchain "
                "(python, node, uv, pnpm); the project's own dependencies are set up before "
                "verification from the repo's ``.devcontainer/devcontainer.json``. Run the test "
                "and lint commands directly; do NOT spend rounds reinstalling dependencies. "
                "If this repo has NO ``.devcontainer/devcontainer.json`` and your work needs the "
                "project's dependencies or a specific toolchain installed, CREATE a minimal one "
                "as part of your deliverable — a ``devcontainer.json`` whose ``postCreateCommand`` "
                "installs the deps — so verification (and every future run) can set the "
                "environment up. It is a real, committed file the repo then owns.\n"
                "9. GREEN BEFORE YOU FINISH. Before sending your plain-text summary (which exits "
                "the loop), run every command check you declared via ``declare_verification`` — "
                "plus every lint/format command — and confirm each exits 0. These are separate "
                "checks; passing one is not passing all. The deliverable is auto-rejected if any "
                "verifier check is red when you summarize.\n"
                "10. DECLARE VERIFICATION FIRST, THEN TEST-FIRST (TDD) — before you write "
                "implementation code, call ``declare_verification`` to commit to HOW this step "
                "will be checked: the command check(s) that must exit 0 (the test command, the "
                "lint command, the build command — declare each that applies as a separate "
                "command check), and/or judge criteria for non-executable work (docs, design). "
                "The verifier executes exactly what you declare, so declare commands you will "
                "actually make pass. A check that only compiles or imports a file (py_compile, "
                "a bare import) does NOT exercise its behaviour — when the step has tests, "
                "declare a command that RUNS the test runner, never one that merely compiles "
                "the test file. Then test-first: (a) write the test expressing the expected "
                "input → output, (b) run it and CONFIRM it fails for the right reason — missing "
                "behaviour, not a syntax error, (c) implement the minimum code to make it pass, "
                "(d) re-run until green. Declaring the contract first nails what 'done' means "
                "before the implementation drifts — the highest-leverage habit for passing "
                "verification on the first round. For a pure-docs/config step with no behaviour "
                "to pin, still declare a judge check with concrete criteria.\n"
                "11. NO SCRATCH SCRIPTS — do NOT create ad-hoc ``run_tests.py`` / "
                "``validate_*.py`` / ``verify_*.py`` helper files to check your own work. The "
                "verifier runs the test + lint tools itself. Such files are not deliverables, "
                "they pollute the workspace, and the linter will flag them. Invoke the test "
                "runner directly via shell_exec instead.\n"
                "12. BUILD EXACTLY WHAT THE DIRECTION ASKS — no more, no less. The Request intent "
                "and work step objective are the contract. Do NOT add infrastructure, drivers, "
                "async layers, ORMs, abstractions, config systems, or dependencies the Request "
                "did not ask for.\n"
                "   - Match the stated technology literally. If the Request names a specific "
                "technology, library, or storage engine, use exactly that — do not substitute a "
                'heavier or trendier alternative. (Example: a Request that says "SQLite" wants '
                "a SQLite driver, not Postgres plus an async ORM.) Reaching for stacks the "
                "Request never mentioned causes real, avoidable failures.\n"
                '   - Match the stated scale. If the Request says "small", "keep it tight", '
                '"a single file is fine" — honour it. Pick ONE canonical layout and put each '
                "thing in exactly one place; do not scatter the same code across duplicate files.\n"
                "   - Use current, non-deprecated APIs for whatever libraries you use. A "
                "deprecation warning is a quality miss.\n"
                "   - Simpler that satisfies the Request beats sophisticated that exceeds it. "
                "The verifier rewards a working minimal solution, not ambition."
            ),
        },
        {"role": "user", "content": user_block},
    ]


# AGENTS.md — auxiliary, founder-editable per-project conventions. The
# emerging cross-tool convention (AGENTS.md / CLAUDE.md). BSNexus seeds
# a default at workspace provision time; once the repo exists the repo
# owns it. Capped so a runaway file can't blow the prompt budget.
_AGENTS_MD_MAX_CHARS = 4000


def _read_agents_md(workspace_dir: Path | str | None) -> str | None:
    """Return the workspace-root ``AGENTS.md`` contents, or None when
    absent / unreadable / empty. Never raises — a missing or broken
    conventions file just means the universal system prompt stands alone.
    """
    if workspace_dir is None:
        return None
    path = Path(workspace_dir) / "AGENTS.md"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text:
        return None
    if len(text) > _AGENTS_MD_MAX_CHARS:
        text = text[:_AGENTS_MD_MAX_CHARS] + "\n…(truncated)"
    return text


def _build_tool_registry(
    workspace_dir: Path | str | None,
    sandbox_session: SandboxSession | None = None,
) -> ToolRegistry | None:
    if workspace_dir is None:
        return None
    path = Path(workspace_dir)
    if not path.exists():
        logger.warning("dispatch_run_attempt_workspace_missing", workspace=str(path))
        return None
    return ToolRegistry(workspace_dir=path, sandbox=sandbox_session)


# G6.7 — pass a short workspace tree summary in the first user
# message so the model doesn't burn rounds listing files just to learn
# what exists. Recursive, ~120 entries, depth 3, ignores cache dirs.
_OVERVIEW_IGNORE = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "node_modules",
    "dist",
    "build",
}
_OVERVIEW_MAX_ENTRIES = 120
_OVERVIEW_MAX_DEPTH = 3
_OVERVIEW_MAX_PREVIEW_FILES = 10
_OVERVIEW_PREVIEW_CHARS = 900
_OVERVIEW_PREVIEW_SUFFIXES = {".md", ".py", ".toml", ".json", ".txt", ".yaml", ".yml"}


def _workspace_overview(workspace_dir: Path | str | None) -> str:
    if workspace_dir is None:
        return ""
    root = Path(workspace_dir)
    if not root.exists():
        return ""
    entries: list[str] = []
    preview_paths: list[Path] = []

    def _walk(current: Path, depth: int) -> None:
        if depth > _OVERVIEW_MAX_DEPTH or len(entries) >= _OVERVIEW_MAX_ENTRIES:
            return
        try:
            children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for child in children:
            if child.name in _OVERVIEW_IGNORE or child.name.startswith("."):
                continue
            relative = child.relative_to(root).as_posix()
            entries.append(relative + ("/" if child.is_dir() else ""))
            if (
                child.is_file()
                and len(preview_paths) < _OVERVIEW_MAX_PREVIEW_FILES
                and child.suffix in _OVERVIEW_PREVIEW_SUFFIXES
            ):
                preview_paths.append(child)
            if len(entries) >= _OVERVIEW_MAX_ENTRIES:
                return
            if child.is_dir():
                _walk(child, depth + 1)

    _walk(root, 1)
    if not entries:
        return "(workspace is empty — you will be creating files from scratch)"
    overview = ["Tree:", *entries]
    if preview_paths:
        overview.append("")
        overview.append("Seed file previews:")
        for path in preview_paths:
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            preview = content[:_OVERVIEW_PREVIEW_CHARS]
            if len(content) > _OVERVIEW_PREVIEW_CHARS:
                preview += "\n...<truncated>"
            overview.append(f"--- {relative} ---")
            overview.append(preview)
    return "\n".join(overview)


async def _aspect_feedback_retry_loop(
    *,
    attempt: RunAttempt,
    request: Request,
    workspace_dir: Path | str,
    output_text: str,
    written_paths: list[str],
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    model: str,
    executor: ExecutorClient,
    tool_registry: ToolRegistry | None,
    session: AsyncSession,
    sandbox_session: SandboxSession | None = None,
) -> tuple[str, list[str]]:
    """Re-enter ``_run_work_phase`` up to ``MAX_ASPECT_RETRIES`` times
    when the model converged but verification aspects failed.

    Each retry: probe blocking aspects via ``probe_aspects`` (no DB
    persistence; idempotent), if any failed inject the failure as a
    user message + the model's prior summary as an assistant turn,
    and run the work phase for ``MAX_ASPECT_RETRY_ROUNDS`` more rounds.
    Stops when aspects pass or retries exhaust.

    Returns the final ``(output_text, written_paths)`` — written_paths
    accumulates across retries. Telemetry records ``aspect_retries``."""
    # Lazy import: ``run_attempt_executor`` is imported by
    # ``verification`` indirectly via orchestration; the deferred import
    # mirrors the circular-import break we set up for ``advance_request_after_proof``.
    from backend.execution.verification import probe_aspects  # noqa: PLC0415

    retries = 0
    while retries < MAX_ASPECT_RETRIES:
        try:
            results = await probe_aspects(
                workspace_root=workspace_dir,
                deliverable_type=DeliverableType.code,
                changed_files=tuple(written_paths),
                verification_contract=(
                    tool_registry.declared_contract if tool_registry is not None else None
                ),
                judge=JudgeContext(executor=executor, model=model or "", metadata=metadata),
                sandbox_session=sandbox_session,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "aspect_probe_crashed_during_retry_loop",
                run_attempt_id=str(attempt.id),
                request_id=str(request.id),
                error=str(exc),
            )
            break

        failures = [r for r in results if r.blocking and r.status != ProofAspectStatus.passed]
        if not failures:
            break

        # Budget cap: only retry if enough work-round budget remains for
        # a meaningful work→test→feedback pass. Below the floor, give up
        # and route through Tier 1 — "aspect_retry_budget_exhausted" is
        # a budget-class terminator, so _execute_one_attempt either
        # spawns a fresh continuation (full budget) or, at the
        # continuation cap, escalates to a founder Decision. Burning the
        # last few rounds on a doomed partial retry helps nobody.
        work_rounds = int(
            (attempt.telemetry or {}).get("phase_rounds", {}).get(RunAttemptPhase.work.value, 0)
        )
        remaining = PHASE_ROUND_BUDGETS[RunAttemptPhase.work] - work_rounds
        if remaining < ASPECT_RETRY_BUDGET_FLOOR:
            logger.info(
                "aspect_retry_budget_floor_reached",
                run_attempt_id=str(attempt.id),
                request_id=str(request.id),
                work_rounds=work_rounds,
                remaining=remaining,
                retries=retries,
            )
            telemetry = dict(attempt.telemetry or {})
            telemetry["aspect_retries"] = retries
            attempt.telemetry = telemetry
            await finish_run_attempt(
                attempt=attempt,
                status=RunAttemptStatus.timed_out,
                terminal_reason="aspect_retry_budget_exhausted",
                session=session,
            )
            raise _ToolLoopTerminated(
                attempt=attempt,
                reason="aspect_retry_budget_exhausted",
                written_paths=written_paths,
                final_text=output_text,
            )

        retries += 1
        logger.info(
            "aspect_feedback_retry",
            run_attempt_id=str(attempt.id),
            retry=retries,
            failures=[f.aspect_type.value for f in failures],
        )

        feedback = _format_aspect_feedback(failures, retries_left=MAX_ASPECT_RETRIES - retries)
        messages.append({"role": "assistant", "content": output_text or "(continued)"})
        messages.append({"role": "user", "content": feedback})

        try:
            output_text, written_paths = await _run_work_phase(
                attempt=attempt,
                messages=messages,
                metadata=metadata,
                model=model,
                executor=executor,
                tool_registry=tool_registry,
                workspace_dir=workspace_dir,
                session=session,
                max_iterations=MAX_ASPECT_RETRY_ROUNDS,
                initial_written_paths=written_paths,
            )
        except _ToolLoopTerminated:
            # Retry consumed its budget mid-fix. ``finish_run_attempt``
            # already ran inside ``_run_work_phase`` (the attempt is
            # now ``terminal``), so the outer dispatcher MUST take the
            # partial-work path next, not the natural-exit path — its
            # next call is ``advance_phase`` which would crash on a
            # terminal attempt. Re-raise so the outer ``except
            # _ToolLoopTerminated`` handler stamps the partial deliverable.
            # ``_run_work_phase`` already carried the accumulated
            # written_paths into ``terminated.written_paths``; just
            # record the partial retry count before re-raising.
            telemetry = dict(attempt.telemetry or {})
            telemetry["aspect_retries"] = retries
            attempt.telemetry = telemetry
            await session.flush()
            raise

    # New dict reference so SQLAlchemy detects the JSON change (the
    # default Mapped[dict] without MutableDict wrapping doesn't track
    # in-place mutations).
    telemetry = dict(attempt.telemetry or {})
    telemetry["aspect_retries"] = retries
    attempt.telemetry = telemetry
    await session.flush()
    return output_text, written_paths


# Exit codes whose process leaves no usable stderr — the kernel kills
# it (or it never started), so the number is the only signal. Each is
# translated to one factual line so the model isn't left guessing at a
# silently-truncated log. These are environment/contract failures, NOT
# code defects.
_SILENT_EXIT_HINTS: dict[int, str] = {
    137: (
        "exit 137 — the process was killed (SIGKILL), almost always out of memory: "
        "the command is too heavy for the sandbox. This is NOT a code defect — "
        "declare a lighter verification command."
    ),
    143: "exit 143 — the process was terminated (SIGTERM) before it finished.",
    124: "exit 124 — the command timed out. Declare a faster verification command.",
    127: (
        "exit 127 — command not found: the tool is not installed in this "
        "environment. This is NOT a code defect — declare a command that exists."
    ),
    126: "exit 126 — command found but not executable.",
}

_ASPECT_SUMMARY_CAP = 3000


def _format_aspect_feedback(failures, retries_left: int) -> str:
    """Compose a user message reporting the verifier's *actual* output.

    No pre-judged "fix your code" framing: a verification failure is one
    of two things and the model must decide which from the facts —
    (1) the code is wrong → fix the workspace files; or (2) the declared
    verification *command* cannot run in this environment (wrong
    toolchain, missing dependency, too heavy) → declare a command that
    does run. Conflating the two is why an environment failure (OOM,
    ModuleNotFound) never converges — you cannot fix an OOM by editing
    code. Silent-kill exit codes get a one-line factual translation
    since the process leaves no log; long output is tail-truncated
    (a failing build/test log puts the cause last)."""
    parts = [
        "The verification aspects for this work step did not pass. A failure "
        "here is one of TWO things — decide which from the output below: "
        "(1) your CODE is wrong → fix the workspace files; or (2) the "
        "verification COMMAND you declared cannot run in this environment "
        "(wrong toolchain, missing dependency, too heavy / OOM) → re-declare "
        "a command that actually runs, scoped to the files you changed. "
        "Then re-run it yourself to confirm before your final summary."
    ]
    for failure in failures:
        parts.append(
            f"\n[{failure.aspect_type.value}] status={failure.status.value} exit_code={failure.exit_code}"
        )
        hint = _SILENT_EXIT_HINTS.get(failure.exit_code)
        if hint is not None:
            parts.append(hint)
        summary = failure.summary or ""
        if not summary:
            parts.append("(no output captured)")
        elif len(summary) > _ASPECT_SUMMARY_CAP:
            # Tail, not head — a failing build/test log puts the cause last.
            parts.append(
                "…(output truncated — showing the tail)\n" + summary[-_ASPECT_SUMMARY_CAP:]
            )
        else:
            parts.append(summary)
    parts.append(
        f"\nYou have {retries_left} aspect-retry round(s) left after this turn. "
        "Use them — silent re-summary without addressing the failures wastes them."
    )
    return "\n".join(parts)


class _ToolLoopTerminated(Exception):
    """Raised inside the work-phase loop when ``record_tool_event``
    fires a terminal reason (budget / repetition). The outer handler
    transitions the work step and returns the dispatcher result.

    ``written_paths`` carries any ``file_write`` targets that *did*
    land before termination so the dispatcher can surface them as a
    ``human_review_required`` deliverable instead of dropping the work
    silently.
    """

    def __init__(
        self,
        *,
        attempt: RunAttempt,
        reason: str,
        written_paths: list[str] | None = None,
        final_text: str = "",
    ) -> None:
        super().__init__(reason)
        self.attempt = attempt
        self.reason = reason
        self.written_paths = list(written_paths or [])
        self.final_text = final_text


async def _run_work_phase(
    *,
    attempt: RunAttempt,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    model: str,
    executor: ExecutorClient,
    tool_registry: ToolRegistry | None,
    workspace_dir: Path | str | None,
    session: AsyncSession,
    max_iterations: int | None = None,
    initial_written_paths: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Tool-call loop for the work phase. Returns ``(final_text,
    written_paths)`` — the model's final plain-text response (persisted
    as the deliverable summary) and the de-duplicated, first-seen-order
    list of every ``file_write`` target (persisted as the deliverable's
    ``artifact_refs`` so the verifier and the G8.2 commit step know
    what the run produced).

    The loop is bounded by both the per-phase ``record_tool_event``
    budget (already wired into the phase machine) and
    ``max_iterations`` (default ``MAX_WORK_LOOP_ITERATIONS``) as an
    outer safety net. ``initial_written_paths`` lets the aspect-
    feedback retry loop continue from a prior pass's accumulated
    artifacts without losing them.
    """
    work_tools = list(ALLOWED_TOOLS_BY_PHASE[RunAttemptPhase.work])
    tools_schema = tool_registry.schema_for(work_tools) if tool_registry is not None else None
    workspace_dir_str = str(workspace_dir) if workspace_dir is not None else None
    final_text = ""
    written_paths: list[str] = list(initial_written_paths or [])
    no_work_nudges = 0
    iteration_cap = max_iterations if max_iterations is not None else MAX_WORK_LOOP_ITERATIONS

    for _ in range(iteration_cap):
        executor_result = await executor.execute(
            messages=messages,
            metadata=metadata,
            model=model,
            workspace_dir=workspace_dir_str,
            tools=tools_schema,
        )
        final_text = str(executor_result.get("output_ref") or "")
        tool_calls = executor_result.get("tool_calls")

        if tool_registry is None:
            return final_text, written_paths

        if not tool_calls:
            if written_paths:
                return final_text, written_paths
            if no_work_nudges < MAX_NO_WORK_NUDGES:
                no_work_nudges += 1
                messages.append({"role": "assistant", "content": final_text or "(no tool calls)"})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You have not created or modified any file yet. A prose answer is not a deliverable. "
                            "Use file_write now to create or modify the required file, then run shell_exec if a "
                            "verifier is available. If you need context, use file_read or file_list first."
                        ),
                    }
                )
                continue
            await finish_run_attempt(
                attempt=attempt,
                status=RunAttemptStatus.failed,
                terminal_reason="failed_nonconvergent:no_workspace_write",
                session=session,
            )
            raise _ToolLoopTerminated(
                attempt=attempt, reason="failed_nonconvergent:no_workspace_write"
            )

        messages.append(_assistant_tool_call_message(final_text, tool_calls))

        for call in tool_calls:
            tool_name = str(call.get("name") or "")
            call_id = str(call.get("id") or "")
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}

            tool_output, exit_code, writes = await _invoke_tool_safely(
                tool_registry, tool_name, arguments
            )
            if tool_name in ("file_write", "file_edit") and exit_code == 0 and writes:
                for path in writes:
                    if path not in written_paths:
                        written_paths.append(path)
            event_input = ToolEventInput(
                tool_name=tool_name,
                args=arguments,
                args_summary=_short_args_summary(tool_name, arguments),
                result_summary=tool_output[:200],
                exit_code=exit_code,
                writes=writes,
            )
            try:
                event_result = await record_tool_event(
                    attempt=attempt, event_input=event_input, session=session
                )
            except Exception as exc:
                # Tool not allowed in this phase / RunAttempt already
                # terminal, etc. — surface and stop. Finish the attempt
                # here, consistently with the no-write and iteration-cap
                # raise sites: otherwise the attempt is left ``running``
                # (a zombie row) while the handler only fails the
                # WorkStep. The exception message is logged because the
                # ``terminal_reason`` only keeps the class name.
                logger.warning(
                    "tool_event_record_failed",
                    run_attempt_id=str(attempt.id),
                    tool_name=tool_name,
                    phase=attempt.phase.value,
                    error_class=exc.__class__.__name__,
                    error=str(exc),
                )
                if attempt.status == RunAttemptStatus.running:
                    await finish_run_attempt(
                        attempt=attempt,
                        status=RunAttemptStatus.failed,
                        terminal_reason=f"tool_event_error:{exc.__class__.__name__}",
                        session=session,
                    )
                raise _ToolLoopTerminated(
                    attempt=attempt,
                    reason=f"tool_event_error:{exc.__class__.__name__}",
                    written_paths=written_paths,
                    final_text=final_text,
                ) from exc

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": tool_output,
                }
            )
            if event_result.terminated:
                raise _ToolLoopTerminated(
                    attempt=attempt,
                    reason=event_result.terminal_reason or "work_loop_terminated",
                    written_paths=written_paths,
                    final_text=final_text,
                )
            # Tier 1 soft pressure: a few rounds before the work-round
            # budget, tell the model to land its current change cleanly
            # and stop, rather than being hard-cut mid-edit. Fires once
            # per attempt (work rounds only ever increment by 1, so the
            # equality check crosses exactly once even across aspect-
            # retry re-entries that share the same cumulative counter).
            work_rounds = int(
                (attempt.telemetry or {}).get("phase_rounds", {}).get(RunAttemptPhase.work.value, 0)
            )
            if work_rounds == PHASE_ROUND_BUDGETS[RunAttemptPhase.work] - SOFT_PRESSURE_HEADROOM:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "BUDGET NEARLY EXHAUSTED — you have a few rounds left. Finish the "
                            "change you are on and bring the workspace to a clean, consistent "
                            "state. Do NOT start new work. If the step is complete, run the "
                            "verifier and send your final plain-text summary now."
                        ),
                    }
                )
            if event_result.nudge:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"{event_result.nudge} You have already seen this tool result. "
                            "Do not repeat the same read/list call. If no file has been written yet, "
                            "use file_write with the required change now; otherwise send the final summary."
                        ),
                    }
                )

    # Outer cap hit without convergence — terminate.
    await finish_run_attempt(
        attempt=attempt,
        status=RunAttemptStatus.timed_out,
        terminal_reason="work_loop_iteration_cap",
        session=session,
    )
    raise _ToolLoopTerminated(
        attempt=attempt,
        reason="work_loop_iteration_cap",
        written_paths=written_paths,
        final_text=final_text,
    )


async def _invoke_tool_safely(
    registry: ToolRegistry, name: str, arguments: dict[str, Any]
) -> tuple[str, int, list[str]]:
    """Run ``registry.invoke`` and translate failures into a string
    the LLM can read. Returns (output, exit_code, writes)."""
    writes: list[str] = []
    if name in ("file_write", "file_edit"):
        path = arguments.get("path")
        if isinstance(path, str):
            writes.append(path)
    try:
        output = await registry.invoke(name, arguments)
        return output, 0, writes
    except ToolError as exc:
        return f"ERROR: {exc}", 1, writes


def _assistant_tool_call_message(text: str, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """LiteLLM/OpenAI assistant turn that carries tool_calls. We
    preserve any leading text the model emitted alongside the calls
    (otherwise some providers reject the next turn for ``content``
    being null *and* ``tool_calls`` being present)."""
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": str(call.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(call.get("name") or ""),
                    "arguments": json.dumps(call.get("arguments") or {}),
                },
            }
            for call in tool_calls
        ],
    }


def _short_args_summary(tool_name: str, arguments: dict[str, Any]) -> str:
    if tool_name == "file_write":
        path = arguments.get("path") or "?"
        content = arguments.get("content") or ""
        return f"file_write {path} ({len(content)} chars)"
    if tool_name == "file_edit":
        path = arguments.get("path") or "?"
        old_len = len(arguments.get("old_string") or "")
        return f"file_edit {path} (replace {old_len} chars)"
    if tool_name == "shell_exec":
        return f"shell_exec {str(arguments.get('command') or '')[:120]}"
    return f"{tool_name} {json.dumps(arguments)[:200]}"
