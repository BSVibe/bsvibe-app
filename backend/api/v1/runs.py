"""/api/v1/runs — read API for ExecutionRun rows.

Read-only on the HTTP surface; runs are *created* by the agent loop / workers
(Bundle G), never directly by an HTTP POST.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.execution.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    ExecutionRun,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)

router = APIRouter()


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID
    product_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    status: RunStatus
    created_at: datetime
    updated_at: datetime


class RunTriggerContext(BaseModel):
    """The "outside" that asked for this run, pulled defensively out of the
    run's free-form ``payload``.

    Connector-inbound runs carry a ``TriggerEvent(source=<connector>,
    trigger_kind=webhook)``; the payload may also carry the founder's Direction
    (``intent_text`` / ``text``) and a product slug. Each key is surfaced only
    when present AND a non-empty string — an odd value (number, list) degrades
    to ``None`` so a sparse / malformed payload never 500s the response model.
    """

    model_config = ConfigDict(extra="forbid")

    source: str | None = None
    trigger_kind: str | None = None
    intent_text: str | None = None
    product: str | None = None


class RunDecision(BaseModel):
    """One paused-run Decision: the blocking question + its resolution state.

    The founder resolves it via ``POST /api/v1/checkpoints/{id}/resolve`` (the
    run-detail UI links a PENDING decision to that re-entry point — it does not
    reinvent resolution).
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    decision: str
    question: str
    rationale: str | None = None
    status: DecisionStatus
    resolution: str | None = None
    created_at: datetime


class RunVerification(BaseModel):
    """The latest VerificationResult outcome for the run, if any."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    outcome: VerificationOutcome
    created_at: datetime


class RunDetailResponse(BaseModel):
    """The inspectable run-detail surface (Stitch "Triggered"): the run's
    status + timestamps, its trigger context, its paused-run Decisions, the
    latest verification outcome, and the resulting Deliverable id (so the UI can
    link to its Delivery Report)."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID
    product_id: uuid.UUID | None = None
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    trigger: RunTriggerContext
    decisions: list[RunDecision] = []
    verification: RunVerification | None = None
    deliverable_id: uuid.UUID | None = None


def _opt_str(value: Any) -> str | None:
    """A non-empty string value, else ``None`` (tolerant of odd payload types)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _trigger_context(payload: Any) -> RunTriggerContext:
    """Map the free-form run payload onto the trigger-context fields, defensively."""
    payload = payload if isinstance(payload, dict) else {}
    # The founder's Direction lives under ``intent_text`` (intake) or ``text``
    # (direct submission) — fall back across both.
    intent = _opt_str(payload.get("intent_text")) or _opt_str(payload.get("text"))
    return RunTriggerContext(
        source=_opt_str(payload.get("source")),
        trigger_kind=_opt_str(payload.get("trigger_kind")),
        intent_text=intent,
        product=_opt_str(payload.get("product")),
    )


def _question_text(decision: Decision) -> str:
    payload = decision.payload or {}
    if isinstance(payload, dict):
        value = payload.get("question")
        if isinstance(value, str):
            return value
    return ""


@router.get("")
async def list_runs(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = 50,
) -> list[RunResponse]:
    """List recent ExecutionRun rows for the workspace, newest first."""
    limit = max(1, min(limit, 200))
    stmt = (
        select(ExecutionRun)
        .where(ExecutionRun.workspace_id == workspace_id)
        .order_by(ExecutionRun.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [
        RunResponse(
            id=row.id,
            workspace_id=row.workspace_id,
            product_id=row.product_id,
            request_id=row.request_id,
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


@router.get("/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunResponse:
    """Fetch one ExecutionRun by id, scoped to the caller's workspace."""
    row = await session.get(ExecutionRun, run_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    return RunResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        product_id=row.product_id,
        request_id=row.request_id,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/{run_id}/detail")
async def get_run_detail(
    run_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunDetailResponse:
    """The inspectable run-detail surface for one ExecutionRun (Stitch
    "Triggered"), scoped to the caller's workspace.

    Bundles the run's trigger context (defensively read out of the free-form
    ``payload``), its paused-run Decisions (the blocking questions the founder
    resolves via /api/v1/checkpoints), the latest VerificationResult outcome,
    and the resulting Deliverable id (so the UI can link to its Delivery
    Report). A cross-workspace / unknown id is 404, never a leak; a run with a
    sparse payload degrades to a calm minimal detail rather than erroring.
    """
    run = await session.get(ExecutionRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")

    decisions_stmt = (
        select(Decision)
        .where(Decision.run_id == run_id, Decision.workspace_id == workspace_id)
        .order_by(Decision.created_at.desc())
    )
    decision_rows = (await session.execute(decisions_stmt)).scalars().all()

    latest_verification_stmt = (
        select(VerificationResult)
        .where(
            VerificationResult.run_id == run_id,
            VerificationResult.workspace_id == workspace_id,
        )
        .order_by(VerificationResult.created_at.desc())
        .limit(1)
    )
    verification_row = (await session.execute(latest_verification_stmt)).scalars().first()

    latest_deliverable_stmt = (
        select(Deliverable.id)
        .where(
            Deliverable.run_id == run_id,
            Deliverable.workspace_id == workspace_id,
        )
        .order_by(Deliverable.created_at.desc())
        .limit(1)
    )
    deliverable_id = (await session.execute(latest_deliverable_stmt)).scalars().first()

    return RunDetailResponse(
        id=run.id,
        workspace_id=run.workspace_id,
        product_id=run.product_id,
        status=run.status,
        created_at=run.created_at,
        updated_at=run.updated_at,
        trigger=_trigger_context(run.payload),
        decisions=[
            RunDecision(
                id=d.id,
                decision=d.decision,
                question=_question_text(d),
                rationale=d.rationale,
                status=d.status,
                resolution=d.resolution,
                created_at=d.created_at,
            )
            for d in decision_rows
        ],
        verification=(
            RunVerification(
                id=verification_row.id,
                outcome=verification_row.outcome,
                created_at=verification_row.created_at,
            )
            if verification_row is not None
            else None
        ),
        deliverable_id=deliverable_id,
    )
