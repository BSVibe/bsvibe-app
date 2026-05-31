"""Executor terminal + audit helpers — shared loop-end machinery.

Extracted from :mod:`backend.executors.orchestrator` in Lift D (§17.8 4-file
split). The three terminal kinds the executor coordinator + verifier produce —
``system_error`` (``_fail``), ``needs_decision`` (``_create_decision`` +
``_decision_result``), and the ``LoopTerminal`` / ``DecisionPending`` audit
emit (``_audit``) — live here so both the dispatch flow
(:mod:`backend.executors.coordinator`) and the verification convergence
(:mod:`backend.executors.verify_handoff`) reuse a single source of truth.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution.audit_events import (
    DecisionPending,
    LoopTerminal,
)
from backend.execution.db import (
    Decision,
    ExecutionRun,
    RunAttempt,
    RunAttemptPhase,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.orchestrator import LoopResult
from backend.supervisor.audit.events import AuditActor, AuditEventBase, AuditResource
from backend.supervisor.audit.service import safe_emit

logger = structlog.get_logger(__name__)


def _utcnow() -> Any:
    from datetime import UTC, datetime  # noqa: PLC0415 — local to avoid top-level churn

    return datetime.now(tz=UTC)


async def fail_terminal(
    session: AsyncSession,
    run: ExecutionRun,
    work_step: WorkStep,
    attempt: RunAttempt,
    *,
    summary: str,
) -> LoopResult:
    """Flip the run to ``system_error`` and emit the founder-facing terminal.

    Mirrors :meth:`backend.execution.orchestrator.RunOrchestrator._finish_failed`
    — fail loudly, persist + audit, return a ``LoopResult`` the
    :class:`~backend.orchestrator.agent_runner.AgentRunner` maps to FAILED."""
    work_step.status = WorkStepStatus.FAILED
    attempt.phase = RunAttemptPhase.FAILED
    attempt.finished_at = _utcnow()
    await session.flush()
    logger.warning("executor_orchestrator_system_error", run_id=str(run.id), error=summary)
    # B15 — terminal: system_error is the founder-facing closing event.
    await emit_audit(
        session,
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


async def create_decision(
    session: AsyncSession,
    run: ExecutionRun,
    *,
    kind: str,
    rationale: str,
    payload: dict[str, Any],
) -> Decision:
    """Persist one :class:`Decision` row for an executor-stuck branch.

    All four executor decision kinds (no transport / no worker / human review /
    verification failed) flow through here so payload + audit emit stay
    uniform."""
    decision = Decision(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=run.workspace_id,
        decision=kind,
        actor_id=None,
        rationale=rationale,
        payload=payload,
    )
    session.add(decision)
    await session.flush()
    logger.info("executor_orchestrator_needs_decision", run_id=str(run.id), kind=kind)
    return decision


async def decision_terminal(
    session: AsyncSession,
    run: ExecutionRun,
    work_step: WorkStep,
    attempt: RunAttempt,
    decision: Decision,
) -> LoopResult:
    """Emit the ``DecisionPending`` + ``needs_decision`` terminal pair.

    Centralised so every executor decision path emits the same pair without each
    caller remembering to. ``decision.payload`` carries the small reason tag
    (``no_executor_*``/``no_verifiable_contract``/``verification_failed``/…).
    The work_step / attempt rows stay exactly as the caller left them — the
    coordinator constructs them RUNNING, and only the verified / fail paths
    advance phase / proof_state. This helper is audit + result envelope only.
    """
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    await emit_audit(
        session,
        run,
        attempt,
        DecisionPending,
        {
            "kind": decision.decision,
            "decision_id": str(decision.id),
            "reason": payload.get("reason"),
        },
    )
    await emit_audit(
        session,
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


async def emit_audit(
    session: AsyncSession,
    run: ExecutionRun,
    attempt: RunAttempt | None,
    event_cls: type[AuditEventBase],
    data: dict[str, Any],
) -> None:
    """Emit one audit event onto the supervisor outbox (B15).

    Mirrors :meth:`backend.execution.orchestrator.RunOrchestrator._audit` so
    the audit-stream surface is uniform across the two compute backends.
    Soft-fail via :func:`safe_emit`."""
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
    await safe_emit(event, session=session)


__all__ = [
    "_utcnow",
    "create_decision",
    "decision_terminal",
    "emit_audit",
    "fail_terminal",
]
