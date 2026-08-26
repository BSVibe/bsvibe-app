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
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from backend.knowledge.extraction.worth_remembering import RememberableKnowledge
    from backend.workflow.application.agent_loop import LoopResult

from backend.config import Settings
from backend.identity.workspaces_db import load_workspace_language
from backend.notifications.copy import (
    NEEDS_YOU_LINK,
    needs_you_reason_body,
    notification_copy,
)
from backend.notifications.emit import emit_notification
from backend.workflow.application._verified_summary import (
    _changed_paths_for,
    _compose_verified_summary,
)
from backend.workflow.application.audit_events import LoopTerminal
from backend.workflow.domain.verified_deliverable import write_verified_deliverable
from backend.workflow.infrastructure.db import (
    Decision,
    Deliverable,
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


def utcnow() -> Any:
    from datetime import UTC, datetime  # noqa: PLC0415 — local to avoid top-level churn

    return datetime.now(tz=UTC)


async def record_activity(
    session: AsyncSession,
    run: ExecutionRun,
    # ``None`` is what the MCP transport passes: an executor's work-tool call happens
    # OUTSIDE any loop attempt (the same reason ``create_decision`` takes an optional
    # work step). The run timeline queries by run, not by attempt, so the id is
    # informational and its absence costs nothing.
    attempt: RunAttempt | None,
    activity_type: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        ExecutionRunActivity(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            activity_type=activity_type,
            payload={"attempt_id": str(attempt.id) if attempt is not None else None, **payload},
        )
    )


async def create_decision(
    session: AsyncSession,
    run: ExecutionRun,
    # Unused by the Decision row (kept positionally for the loop's call sites). ``None`` is
    # what the MCP transport passes: an executor asks the founder OUT OF BAND, from outside
    # any loop WorkStep (T1b).
    work_step: WorkStep | None,
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
    # Every path a run stops on a Decision passes through here — so this is the
    # ONE place the founder is called (Notifier §D4). Stage the ``needs_you``
    # notification in the SAME transaction that creates the Decision: it is
    # confirmed iff the Decision commits (a rolled-back Decision leaves no ghost
    # notification), and a crash after commit still leaves the outbox row for the
    # NotifyWorker to drain (no lost notification). Direct SEND stays with the
    # worker — never inside this write path (Notifier §D3).
    await _emit_needs_you(session, run, decision)
    logger.info("run_orchestrator_needs_decision", run_id=str(run.id), kind=kind)
    return decision


async def _emit_needs_you(session: AsyncSession, run: ExecutionRun, decision: Decision) -> None:
    """Stage the ``needs_you`` outbox row for a just-created Decision.

    The push ``title``/``body`` are rendered by the localized notification-copy
    catalog in the workspace's ``workspaces.language`` (KO/EN). When the Decision
    carries the founder's own blocking ``question`` (``ask_user_question``) it
    rides through verbatim as the ``detail``. For a SYSTEM-minted Decision with no
    question (verify-gate / ``human_review_required``), the body is derived from
    the machine ``reason`` via the localized copy catalog — NEVER the raw English
    ``decision.rationale`` (which leaked verifier jargon like "weak evidence
    (grade D)" to KO founders). Delegates to the shared
    :func:`~backend.notifications.emit.emit_notification` seam (savepoint +
    dedupe): the UNIQUE ``dedupe_key`` (``needs_you:<decision_id>``) makes a
    re-emit of the same Decision's notification a DB-level no-op, so the founder
    is called exactly once per Decision even under a retried ``create_decision``.
    """
    payload_in = decision.payload or {}
    language = await load_workspace_language(session, run.workspace_id)
    question = str(payload_in.get("question") or "").strip()
    # A founder question rides through verbatim; a system-minted Decision (no
    # question) maps its machine ``reason`` to friendly localized copy instead of
    # leaking the English ``decision.rationale``.
    detail = question or needs_you_reason_body(str(payload_in.get("reason") or ""), language)
    copy = notification_copy("needs_you", language, detail=detail)
    await emit_notification(
        session,
        workspace_id=run.workspace_id,
        event="needs_you",
        dedupe_key=f"needs_you:{decision.id}",
        payload={
            "title": copy.title,
            "body": copy.body,
            "link": NEEDS_YOU_LINK,
            "run_id": str(run.id),
            "decision_id": str(decision.id),
        },
        producer_id="workflow:create_decision",
    )


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


async def land_verified_artifacts(
    session: AsyncSession,
    *,
    run: ExecutionRun,
    attempt_id: uuid.UUID,
    written_paths: list[str],
    final_text: str,
    verdict_result: Mapping[str, Any] | None,
    redis_client: Any,
    settings: Settings,
    knowledge: RememberableKnowledge | None = None,
) -> Deliverable:
    """Land the run's finished work where the FOUNDER can reach it.

    Deliverable + DeliveryEventRow + settle activity + the Redis wake-up — the
    artifact contract that puts an item in the Safe Mode queue, sends the
    telegram, opens the PR (#738) and feeds the Brief. :func:`finish_verified`
    already declared this contract "the SAME regardless of compute backend";
    this is that sentence made true, because client_attach was NOT going through
    it and so a finished run on the founder's own machine reached nobody.

    Deliberately NOT included, and left to each caller: the WorkStep /
    RunAttempt transitions, and above all ``proof_state``. The sandbox terminal
    sets ``PROVED`` because its call site has already observed a PASSED verdict;
    a client_attach run only earns that when its gate actually RAN and passed.
    Sharing the landing must never import the proof claim (trust ratchet).

    The settle payload carries the run's STABLE context (product binding +
    founder intent_text) so the SettleWorker can cluster garden observations by
    product + intent — deterministic inputs, never the work LLM's free output.
    """
    # The deliverable summary's fixed chrome (changed-file header, verification
    # sentence) is localized to the workspace language so a KO founder's delivered
    # summary / Telegram body reads in Korean, not English.
    language = await load_workspace_language(session, run.workspace_id)
    # The file list the summary reports must come from the SAME place the
    # deliverable's diff does (``main...HEAD``), or the two disagree — prod run
    # `02af81f7` shipped "바뀐 파일 4개" against a 2-file PR. ``None`` when it
    # cannot be determined; the summary then labels the written paths honestly
    # as "touched" instead of claiming they changed.
    changed_paths = await _changed_paths_for(run)
    deliverable = await write_verified_deliverable(
        session,
        run,
        attempt_id=attempt_id,
        artifact_refs=written_paths,
        # Title the summary by the founder intent + body by the changed files,
        # not the work LLM's raw narration — the first line becomes the PR
        # title + settle note title. R1: weave in what the verifier proved.
        summary=_compose_verified_summary(
            run, final_text, written_paths, verdict_result, language, changed_paths=changed_paths
        ),
        # v2 — the agent's own retrospective knowledge declaration (or None).
        knowledge=knowledge,
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
    return deliverable


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
    knowledge: RememberableKnowledge | None = None,
) -> LoopResult:
    """Land the verified terminal — the sandbox path's PROVED terminal.

    Owns what is specific to a run the SERVER verified: the WorkStep /
    RunAttempt transitions and the ``PROVED`` proof_state (justified by the
    PASSED verdict this function's call site has already observed). The artifact
    contract itself — Deliverable + DeliveryEventRow + settle activity + Redis
    wake-up — is the SAME regardless of compute backend and lives in
    :func:`land_verified_artifacts`, which client_attach lands through too.
    """
    from backend.workflow.application.agent_loop import (  # noqa: PLC0415 — cycle break
        LoopResult,
    )

    work_step.status = WorkStepStatus.VERIFIED
    work_step.proof_state = ProofState.PROVED
    attempt.phase = RunAttemptPhase.COMPLETED
    attempt.finished_at = utcnow()

    await land_verified_artifacts(
        session,
        run=run,
        attempt_id=attempt.id,
        written_paths=written_paths,
        final_text=final_text,
        verdict_result=verdict.result if isinstance(verdict.result, Mapping) else None,
        redis_client=redis_client,
        settings=settings,
        knowledge=knowledge,
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
    "land_verified_artifacts",
    "record_activity",
    "utcnow",
]
