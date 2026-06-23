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

import re
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import LoopResult

from backend.config import Settings
from backend.workflow.application.audit_events import LoopTerminal
from backend.workflow.domain.verified_deliverable import write_verified_deliverable
from backend.workflow.infrastructure.db import (
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
from plugin.audit.events import AuditActor, AuditEventBase, AuditResource
from plugin.audit.service import safe_emit

logger = structlog.get_logger(__name__)

# A coding-agent executor's ``--print`` output ends with a machine-readable
# ``<verification-contract>{…}</…>`` block (Lift E30). It's noise in a
# human-facing deliverable summary / PR body, so strip it.
_CONTRACT_BLOCK_RE = re.compile(
    r"<verification-contract>.*?</verification-contract>",
    re.DOTALL | re.IGNORECASE,
)
#: Cap the title line (first line of the summary → PR title / settle note
#: title) so a single-line intent doesn't produce a 512-char title.
_MAX_SUMMARY_TITLE = 120
#: Repairs streaming chunk-join whitespace artifacts ("done.Next" → "done. Next")
#: in the fallback prose — the coding-agent ``--print`` output concatenates
#: streamed chunks without the inter-sentence space.
_CHUNK_JOIN_RE = re.compile(r"([.!?:])([A-Z])")


def _compose_verified_summary(
    run: ExecutionRun, final_text: str, written_paths: Sequence[str] | None = None
) -> str:
    """Build the verified deliverable's summary — titled by the founder INTENT,
    bodied by the DETERMINISTIC list of changed files.

    The summary's first line becomes the PR title (via ``_split_summary``) and
    the settle note's title. The work LLM's ``final_text`` is raw first-person
    streaming narration ("I'll invoke /feature-workflow… Now the
    implementation… Phase 1 (RED)…") with chunk-join whitespace artifacts plus
    the E30 contract block — slop in a user-facing deliverable summary / PR body
    (live dogfood F4; earlier garbage PR titles, PR #374). So lead with the
    founder intent (what was asked == what shipped for a verified run) and list
    what actually changed; the agent's prose stays in the ``llm_turn`` activity
    for debugging. ``final_text`` is only a FALLBACK body (contract-stripped,
    whitespace-repaired) when no changed-file list is available — e.g. a
    non-file deliverable. Falls back to a stable title when there is no intent.
    """
    payload = run.payload or {}
    intent = str(payload.get("intent_text") or payload.get("text") or "").strip()
    first_line = next((ln.strip() for ln in intent.splitlines() if ln.strip()), "")
    title = first_line[:_MAX_SUMMARY_TITLE].rstrip() or "Delivered change"

    files = [p.strip() for p in (written_paths or []) if p and p.strip()]
    if files:
        body = "Changed files:\n" + "\n".join(f"- {p}" for p in files)
    else:
        stripped = _CONTRACT_BLOCK_RE.sub("", final_text or "").strip()
        body = _CHUNK_JOIN_RE.sub(r"\1 \2", stripped)
    return f"{title}\n\n{body}" if body else title


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
        # Title the summary by the founder intent + body by the changed files,
        # not the work LLM's raw narration — the first line becomes the PR
        # title + settle note title.
        summary=_compose_verified_summary(run, final_text, written_paths),
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
# pulling :mod:`backend.workflow.application.audit_events` directly (one canonical
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
