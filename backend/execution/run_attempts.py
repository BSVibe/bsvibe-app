from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution._domain import RunAttemptPhase, RunAttemptStatus
from backend.execution.state_machine import can_advance_run_attempt_phase

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.models
# from backend.src.models import RunAttempt, ToolEvent, WorkStep


class RunAttemptStateError(ValueError):
    """Raised when a RunAttempt phase, tool, or terminal transition is invalid."""


ALLOWED_TOOLS_BY_PHASE: dict[RunAttemptPhase, frozenset[str]] = {
    RunAttemptPhase.prepare: frozenset(
        {"file_read", "file_list", "repo_context", "knowledge_search"}
    ),
    RunAttemptPhase.work: frozenset(
        {"file_write", "file_edit", "file_read", "file_list", "shell_exec", "declare_verification"}
    ),
    RunAttemptPhase.verify: frozenset({"verifier_command"}),
    RunAttemptPhase.summarize: frozenset(),
    RunAttemptPhase.terminal: frozenset(),
}

PHASE_ROUND_BUDGETS: dict[RunAttemptPhase, int] = {
    RunAttemptPhase.prepare: 3,
    # ``work`` budget. Cycle 7-14 dogfooding telemetry: every productive
    # work step used 23-33 rounds against the prior 32-budget — i.e. the
    # budget was cutting the model off mid-self-correction, and the
    # "model quality ceiling" narrative was substantially a budget
    # ceiling (qwen3-coder:30b is capable-but-slow). Raised to 48 so the
    # observed 23-33 range finishes comfortably in one RunAttempt; the
    # Tier-1 continuation system (handoff → fresh RunAttempt) absorbs
    # genuine over-runs beyond 48. See
    # ~/Docs/BSNexus_Budget_Handoff_Continuation_Design_2026-05-16.md.
    RunAttemptPhase.work: 48,
    RunAttemptPhase.verify: 1,
    RunAttemptPhase.summarize: 2,
    RunAttemptPhase.terminal: 0,
}

# Total-round catastrophic cap. Must exceed the ``work`` budget plus
# the other phases combined or the work budget never gets a chance
# (work 48 + prepare 3 + verify 1 + summarize 2 = 54, plus headroom).
CATASTROPHIC_ROUND_CAP = 64
REPETITION_WINDOW = 4
REPETITION_TERMINATION_COUNT = 4

LLM_OWNED_FIELDS = frozenset({"summary", "residual_risks", "notes", "suggested_next_steps"})
FORBIDDEN_LLM_SYSTEM_FIELDS = frozenset(
    {
        "blocking",
        "completion_status",
        "decision_blocking",
        "phase",
        "proof_state",
        "request_status",
        "shipped",
        "status",
        "terminal_reason",
        "verified",
        "verifier_type",
    }
)


@dataclass(frozen=True)
class ToolEventInput:
    tool_name: str
    args: dict[str, Any]
    args_summary: str | None = None
    result_summary: str | None = None
    exit_code: int | None = None
    writes: list[str] | None = None


@dataclass(frozen=True)
class ToolEventResult:
    event: ToolEvent
    terminated: bool
    terminal_reason: str | None = None
    nudge: str | None = None


async def create_run_attempt(
    *,
    work_step: WorkStep,
    executor_kind: str,
    model: str | None,
    session: AsyncSession,
) -> RunAttempt:
    attempt = RunAttempt(
        work_step_id=work_step.id,
        executor_kind=executor_kind,
        model=model,
        phase=RunAttemptPhase.prepare,
        status=RunAttemptStatus.running,
        telemetry={
            "phase_rounds": {phase.value: 0 for phase in RunAttemptPhase},
            "ignored_llm_system_fields": [],
            "repetition_nudges": [],
            "termination": None,
        },
    )
    session.add(attempt)
    await session.commit()
    await session.refresh(attempt)
    return attempt


async def advance_phase(
    *,
    attempt: RunAttempt,
    target: RunAttemptPhase,
    session: AsyncSession,
) -> RunAttempt:
    _ensure_running(attempt)
    if not can_advance_run_attempt_phase(attempt.phase, target):
        raise RunAttemptStateError(
            f"Cannot transition RunAttempt from {attempt.phase.value} to {target.value}"
        )
    if target == RunAttemptPhase.terminal:
        raise RunAttemptStateError("Use finish_run_attempt so terminal_reason is explicit")

    attempt.phase = target
    _telemetry(attempt)["phase_rounds"].setdefault(target.value, 0)
    await session.commit()
    await session.refresh(attempt)
    return attempt


async def finish_run_attempt(
    *,
    attempt: RunAttempt,
    status: RunAttemptStatus,
    terminal_reason: str,
    session: AsyncSession,
) -> RunAttempt:
    if not terminal_reason.strip():
        raise RunAttemptStateError("RunAttempt terminal_reason is required")
    if status == RunAttemptStatus.running:
        raise RunAttemptStateError("Terminal RunAttempt status cannot be running")

    attempt.phase = RunAttemptPhase.terminal
    attempt.status = status
    attempt.terminal_reason = terminal_reason.strip()
    attempt.completed_at = datetime.now(UTC)
    telemetry = _telemetry(attempt)
    telemetry["termination"] = {
        "status": status.value,
        "reason": attempt.terminal_reason,
    }
    attempt.telemetry = telemetry
    await session.commit()
    await session.refresh(attempt)
    return attempt


async def record_tool_event(
    *,
    attempt: RunAttempt,
    event_input: ToolEventInput,
    session: AsyncSession,
) -> ToolEventResult:
    _ensure_running(attempt)
    if event_input.tool_name not in ALLOWED_TOOLS_BY_PHASE[attempt.phase]:
        raise RunAttemptStateError(
            f"Tool {event_input.tool_name!r} is not allowed in {attempt.phase.value}"
        )

    telemetry = _telemetry(attempt)
    phase_rounds = telemetry.setdefault("phase_rounds", {})
    phase_rounds[attempt.phase.value] = int(phase_rounds.get(attempt.phase.value, 0)) + 1
    attempt.telemetry = telemetry
    attempt.round_count += 1

    event = ToolEvent(
        run_attempt_id=attempt.id,
        round_index=attempt.round_count,
        tool_name=event_input.tool_name,
        args_hash=hash_tool_args(event_input.args),
        args_summary=event_input.args_summary,
        result_summary=event_input.result_summary,
        exit_code=event_input.exit_code,
        writes=event_input.writes or [],
    )
    session.add(event)
    await session.flush()

    termination_reason = await _terminal_reason_after_event(
        attempt=attempt, event=event, session=session
    )
    nudge = await _record_repetition_nudge_if_needed(attempt=attempt, event=event, session=session)
    if termination_reason is not None:
        await finish_run_attempt(
            attempt=attempt,
            status=RunAttemptStatus.failed
            if "repeated_tool_call" in termination_reason
            else RunAttemptStatus.timed_out,
            terminal_reason=termination_reason,
            session=session,
        )
        await session.refresh(event)
        return ToolEventResult(
            event=event, terminated=True, terminal_reason=termination_reason, nudge=nudge
        )

    await session.commit()
    await session.refresh(attempt)
    await session.refresh(event)
    return ToolEventResult(event=event, terminated=False, nudge=nudge)


def accept_llm_phase_output(*, attempt: RunAttempt, payload: dict[str, Any]) -> dict[str, Any]:
    accepted = {key: value for key, value in payload.items() if key in LLM_OWNED_FIELDS}
    ignored = sorted(key for key in payload if key in FORBIDDEN_LLM_SYSTEM_FIELDS)
    if ignored:
        telemetry = _telemetry(attempt)
        telemetry.setdefault("ignored_llm_system_fields", []).append(
            {
                "phase": attempt.phase.value,
                "fields": ignored,
            }
        )
        attempt.telemetry = telemetry
    return accepted


def hash_tool_args(args: dict[str, Any]) -> str:
    payload = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _terminal_reason_after_event(
    *,
    attempt: RunAttempt,
    event: ToolEvent,
    session: AsyncSession,
) -> str | None:
    if attempt.round_count >= CATASTROPHIC_ROUND_CAP:
        return "catastrophic_round_budget_exceeded"
    phase_rounds = _telemetry(attempt).setdefault("phase_rounds", {})
    if int(phase_rounds.get(attempt.phase.value, 0)) > PHASE_ROUND_BUDGETS[attempt.phase]:
        return f"phase_round_budget_exceeded:{attempt.phase.value}"

    recent_events = (
        (
            await session.execute(
                select(ToolEvent)
                .where(ToolEvent.run_attempt_id == attempt.id)
                .order_by(ToolEvent.round_index.desc())
                .limit(REPETITION_WINDOW)
            )
        )
        .scalars()
        .all()
    )
    repeat_count = sum(
        1
        for recent_event in recent_events
        if recent_event.tool_name == event.tool_name and recent_event.args_hash == event.args_hash
    )
    if repeat_count >= REPETITION_TERMINATION_COUNT:
        return f"failed_nonconvergent:repeated_tool_call:{event.tool_name}"
    return None


async def _record_repetition_nudge_if_needed(
    *,
    attempt: RunAttempt,
    event: ToolEvent,
    session: AsyncSession,
) -> str | None:
    recent_events = (
        (
            await session.execute(
                select(ToolEvent)
                .where(ToolEvent.run_attempt_id == attempt.id)
                .order_by(ToolEvent.round_index.desc())
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if len(recent_events) < 2:
        return None
    if any(
        recent_event.tool_name != event.tool_name or recent_event.args_hash != event.args_hash
        for recent_event in recent_events
    ):
        return None

    nudge = "You already performed this action. Move to summary or stop."
    telemetry = _telemetry(attempt)
    nudges = telemetry.setdefault("repetition_nudges", [])
    if not any(item.get("round_index") == event.round_index for item in nudges):
        nudges.append(
            {
                "round_index": event.round_index,
                "tool_name": event.tool_name,
                "args_hash": event.args_hash,
                "message": nudge,
            }
        )
    attempt.telemetry = telemetry
    return nudge


def _ensure_running(attempt: RunAttempt) -> None:
    if attempt.status != RunAttemptStatus.running or attempt.phase == RunAttemptPhase.terminal:
        raise RunAttemptStateError("RunAttempt is terminal")


def _telemetry(attempt: RunAttempt) -> dict[str, Any]:
    telemetry = deepcopy(attempt.telemetry or {})
    attempt.telemetry = telemetry
    return telemetry
