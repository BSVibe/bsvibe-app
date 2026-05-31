"""Run persistence — DB-side effects of the agent loop.

Lifted from ``backend.execution.orchestrator`` (Lift H2a / v8 §17.1). The
helpers here own the loop's *write* side: appending
:class:`ExecutionRunActivity` rows, opening :class:`Decision` rows,
landing the verified-terminal artifact (:func:`finish_verified`), and
soft-emitting audit events onto the supervisor outbox.

H2a is a mechanical decomposition — no semantic changes. The Repository
extraction (Lift I) will absorb the direct ``session.add`` sites here
into a proper repository; for now they preserve the pre-H2a behaviour
byte-for-byte. The Repository-violation count is UNCHANGED — just
distributed across the new files.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import LoopResult

from backend.config import Settings
from backend.execution.audit_events import LoopTerminal
from backend.execution.db import (
    Decision,
    ExecutionRun,
    ExecutionRunActivity,
    ProofState,
    RunAttempt,
    RunAttemptPhase,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.verified_deliverable import write_verified_deliverable
from plugin.audit.events import AuditActor, AuditEventBase, AuditResource
from plugin.audit.service import safe_emit

logger = structlog.get_logger(__name__)


def utcnow() -> Any:
    from datetime import UTC, datetime  # noqa: PLC0415 — local to avoid top-level churn

    return datetime.now(tz=UTC)


async def record_activity(
    session: AsyncSession,
    run: ExecutionRun,
    attempt: RunAttempt,
    activity_type: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        ExecutionRunActivity(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            activity_type=activity_type,
            payload={"attempt_id": str(attempt.id), **payload},
        )
    )


async def create_decision(
    session: AsyncSession,
    run: ExecutionRun,
    work_step: WorkStep,
    *,
    kind: str,
    payload: dict[str, Any],
    rationale: str,
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
    session.add(decision)
    await session.flush()
    logger.info("run_orchestrator_needs_decision", run_id=str(run.id), kind=kind)
    return decision


def decision_result(
    run: ExecutionRun,
    work_step: WorkStep,
    attempt: RunAttempt,
    decision: Decision,
    written_paths: list[str],
    final_text: str,
) -> LoopResult:
    """Build the ``needs_decision`` :class:`LoopResult`.

    Imported locally to keep this module dependency-free of the loop
    conductor file (``agent_loop.py``) where :class:`LoopResult` lives.
    """
    from backend.workflow.application.agent_loop import (  # noqa: PLC0415 — cycle break
        LoopResult,
    )

    return LoopResult(
        outcome="needs_decision",
        run_id=run.id,
        work_step_id=work_step.id,
        run_attempt_id=attempt.id,
        decision_id=decision.id,
        written_paths=written_paths,
        summary=final_text,
    )


# Invariant: this helper MUST only be called from a code path that has
# already observed ``VerificationOutcome.PASSED`` on the verifier verdict
# (see :mod:`backend.workflow.application._drive_loop`). The helper itself
# does NOT re-check; the gate is at the call site. The structural anti-
# regression in :mod:`tests.execution.test_proved_invariant` grep-pins the
# ``VerificationOutcome.PASSED`` reference in this same file so any future
# wrap-call here remains paired with the gate identifier.
async def finish_verified(
    session: AsyncSession,
    *,
    run: ExecutionRun,
    work_step: WorkStep,
    attempt: RunAttempt,
    written_paths: list[str],
    final_text: str,
    verdict: VerificationResult,
    redis_client: Any,
    settings: Settings,
) -> LoopResult:
    """Land the verified terminal — Deliverable type CODE + Redis wake-up.

    The verified-terminal artifact contract (Deliverable + DeliveryEventRow +
    settle activity) is the SAME regardless of compute backend, so it lives in
    ONE shared helper (Lift 5b). The settle payload carries the run's STABLE
    context (product binding + founder intent_text) so the SettleWorker can
    cluster garden observations by product + intent — deterministic inputs,
    never the work LLM's free output.
    """
    from backend.workflow.application.agent_loop import (  # noqa: PLC0415 — cycle break
        LoopResult,
    )

    work_step.status = WorkStepStatus.VERIFIED
    work_step.proof_state = ProofState.PROVED
    attempt.phase = RunAttemptPhase.COMPLETED
    attempt.finished_at = utcnow()

    deliverable = await write_verified_deliverable(
        session,
        run,
        attempt_id=attempt.id,
        artifact_refs=written_paths,
        summary=final_text,
    )

    # Wake the delivery + settle consumers (worker_mode="redis_streams"
    # only). The DeliveryEventRow + settle ExecutionRunActivity are the
    # source of truth — already flushed above; the XADD is only a wake-up so
    # the consumer ticks immediately instead of waiting for the next DB poll.
    # Gated (no-op + no Redis touched in db_polling — the default) and
    # soft-fail (a Redis hiccup never reverts the verified terminal). DB
    # polling remains the safety net. The emit helper is imported LOCALLY
    # (``backend.workers`` pulls in ``agent_worker`` which imports this
    # module → a module-level import would be a cycle).
    from backend.workers.emit import (  # noqa: PLC0415 — cross-domain, breaks import cycle
        STREAM_DELIVER,
        STREAM_SETTLE,
        emit_stream_notification,
    )

    await emit_stream_notification(
        redis_client,
        settings=settings,
        stream=STREAM_DELIVER,
        fields={"workspace_id": str(run.workspace_id), "deliverable_id": str(deliverable.id)},
    )
    await emit_stream_notification(
        redis_client,
        settings=settings,
        stream=STREAM_SETTLE,
        fields={"workspace_id": str(run.workspace_id), "run_id": str(run.id)},
    )

    logger.info(
        "run_orchestrator_verified",
        run_id=str(run.id),
        artifact_refs=written_paths,
    )
    return LoopResult(
        outcome="verified",
        run_id=run.id,
        work_step_id=work_step.id,
        run_attempt_id=attempt.id,
        verification_result_id=verdict.id,
        written_paths=written_paths,
        summary=final_text,
    )


async def audit_event(
    session: AsyncSession,
    run: ExecutionRun,
    attempt: RunAttempt | None,
    event_cls: type[AuditEventBase],
    data: dict[str, Any],
) -> None:
    """Emit one audit event onto the supervisor outbox (B15).

    The supervisor :class:`backend.workflow.infrastructure.workers.relay_worker.RelayWorker` drains
    the outbox onto the audit stream — exactly the same seam the gateway
    chat path uses. ``safe_emit`` swallows any emitter failure so the run
    is NEVER broken by audit infrastructure trouble (the soft-fail contract
    every audit producer follows).
    """
    actor = AuditActor(type="system", id="backend.execution.run_orchestrator")
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


# Re-export so the agent loop can emit ``LoopTerminal`` events without
# pulling :mod:`backend.execution.audit_events` directly (one canonical
# import sink for the run-persistence concern).
__all__ = [
    "LoopTerminal",
    "audit_event",
    "create_decision",
    "decision_result",
    "finish_verified",
    "record_activity",
    "utcnow",
]
