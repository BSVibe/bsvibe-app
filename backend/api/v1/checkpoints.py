"""/api/v1/checkpoints — founder resolution of paused-run Decisions.

Workflow §5 #4 / §12.5 #8. When the agent loop is stuck or the work LLM
calls ``ask_user_question``, :class:`~backend.execution.orchestrator.RunOrchestrator`
mints an ``execution_decisions`` row and the run pauses (stays RUNNING — not a
DB terminal). This router is the founder's re-entry point:

* ``GET  /api/v1/checkpoints`` — list PENDING execution Decisions for the
  workspace (the blocking questions awaiting a human answer).
* ``POST /api/v1/checkpoints/{id}/resolve`` — record the founder's answer on
  the Decision, fold it into the run payload, and resume the paused run by
  transitioning it RUNNING → OPEN so :meth:`AgentWorker.drive_once` (which
  scans ``status==OPEN`` runs) re-picks it. The orchestrator then injects the
  resolved answer into the loop's initial messages so the work continues with
  the founder's decision in context.

v1 resumes the run *inline* here (not via the event-driven
:class:`~backend.intake.decision_resolution.DecisionResolutionTrigger`, which
remains a future option). This is simpler — no phantom Request, the paused run
is resumed directly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user_row, get_db_session, get_workspace_id
from backend.execution.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)
from backend.identity.db import UserRow
from backend.orchestrator.agent_runner import AgentRunner

router = APIRouter()


class CheckpointResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    decision: str
    question: str
    rationale: str | None = None
    created_at: datetime


class ResolvedCheckpointResponse(BaseModel):
    """One answered paused-run checkpoint (the Decisions "Resolved" tab,
    checkpoint side): the question + the founder's recorded answer + when."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    question: str
    resolution: str | None = None
    resolved_at: datetime | None = None


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(..., min_length=1)


class ResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    status: DecisionStatus
    resolution: str
    resolved_at: datetime
    run_status: RunStatus


# B4: executor B2b Decisions (raised when an executor run does NOT verify) record
# ``payload.reason`` instead of ``payload.question`` — they are an honest "this
# needs you" surfaced as a Decision, not a work-LLM question. Map the kind →
# a calm, human-readable line so the founder never sees a blank question on a
# genuinely actionable needs-you item.
_EXECUTOR_DECISION_QUESTIONS: dict[str, str] = {
    "verification_failed": "BSVibe couldn't verify this work — review it before it ships?",
    "human_review_required": "This work needs your review before BSVibe can call it verified.",
}


def _question_text(decision: Decision) -> str:
    """The founder-facing question for a paused-run Decision.

    Prefers the work LLM's recorded ``payload.question`` (the ``ask_user_question``
    path). For an executor B2b Decision — which records ``payload.reason``, not a
    question — fall back to a calm kind-derived line so the needs-you item is
    never blank. A wholly unrecognised reason-only Decision degrades to an empty
    string (unchanged), never raising."""
    payload = decision.payload or {}
    if isinstance(payload, dict):
        value = payload.get("question")
        if isinstance(value, str) and value.strip():
            return value
    fallback = _EXECUTOR_DECISION_QUESTIONS.get(decision.decision)
    return fallback if fallback is not None else ""


@router.get("")
async def list_checkpoints(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[CheckpointResponse]:
    """List PENDING execution Decisions for the workspace, newest first."""
    stmt = (
        select(Decision)
        .where(
            Decision.workspace_id == workspace_id,
            Decision.status == DecisionStatus.PENDING,
        )
        .order_by(Decision.created_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        CheckpointResponse(
            id=row.id,
            run_id=row.run_id,
            decision=row.decision,
            question=_question_text(row),
            rationale=row.rationale,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/resolved")
async def list_resolved_checkpoints(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ResolvedCheckpointResponse]:
    """List RESOLVED execution Decisions for the Decisions "Resolved" tab,
    most-recently-resolved first (created_at as a stable tiebreaker)."""
    stmt = (
        select(Decision)
        .where(
            Decision.workspace_id == workspace_id,
            Decision.status == DecisionStatus.RESOLVED,
        )
        .order_by(Decision.resolved_at.desc(), Decision.created_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        ResolvedCheckpointResponse(
            id=row.id,
            run_id=row.run_id,
            question=_question_text(row),
            resolution=row.resolution,
            resolved_at=row.resolved_at,
        )
        for row in rows
    ]


@router.post("/{checkpoint_id}/resolve")
async def resolve_checkpoint(
    checkpoint_id: uuid.UUID,
    body: ResolveRequest,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    user_row: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResolveResponse:
    """Resolve a pending Decision with the founder's answer and resume the run.

    404 when the Decision is not in the caller's workspace or is not pending.
    On success: record the answer on the Decision, append it to the run's
    ``payload["resolved_decisions"]``, and transition the run RUNNING → OPEN so
    the worker re-picks it (the loop then sees the answer in its messages).
    """
    decision = await session.get(Decision, checkpoint_id)
    if (
        decision is None
        or decision.workspace_id != workspace_id
        or decision.status is not DecisionStatus.PENDING
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pending checkpoint {checkpoint_id} not found",
        )

    now = datetime.now(tz=UTC)
    decision.status = DecisionStatus.RESOLVED
    decision.resolution = body.answer
    decision.resolved_at = now
    decision.resolved_by = user_row.id

    run = await session.get(ExecutionRun, decision.run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {decision.run_id} for checkpoint not found",
        )

    # Fold the resolution into the run payload so the loop seeds it as context.
    # Re-assign payload (not in-place mutate) so SQLAlchemy detects the change
    # on a JSON column.
    payload: dict[str, Any] = dict(run.payload or {})
    resolved = list(payload.get("resolved_decisions") or [])
    resolved.append(
        {
            "decision_id": str(decision.id),
            "question": _question_text(decision),
            "answer": body.answer,
        }
    )
    payload["resolved_decisions"] = resolved
    run.payload = payload

    await session.flush()

    # Resume: RUNNING → OPEN so AgentWorker.drive_once (scans OPEN runs) re-picks
    # it. AgentRunner.transition no-ops if the run is not RUNNING (e.g. already
    # OPEN), which is harmless — the answer is still recorded + folded in.
    runner = AgentRunner(session)
    await runner.transition(
        run_id=run.id,
        to_status=RunStatus.OPEN,
        reason=f"resumed: decision {decision.id} resolved",
    )
    await session.commit()

    return ResolveResponse(
        id=decision.id,
        run_id=run.id,
        status=decision.status,
        resolution=body.answer,
        resolved_at=now,
        run_status=run.status,
    )
