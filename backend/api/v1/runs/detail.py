"""``GET /api/v1/runs/{run_id}/detail`` — the inspectable run-detail surface.

Bundles the run's trigger context, paused-run decisions, latest verification,
verified-final + mid-loop partial deliverables, and a STORY timeline (real
activity rows preferred; derived from verification+deliverable as a calm
fallback). Strictly read-only — all writes happen inside the agent loop.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.v1._workflow_deps import (
    get_decision_repository,
    get_deliverable_repository,
    get_run_repository,
)
from backend.workflow.domain.repositories import (
    DecisionRepository,
    DeliverableRepository,
    RunRepository,
)
from backend.workflow.domain.verified_deliverable import PARTIAL_DELIVERABLE_KIND
from backend.workflow.infrastructure.db import (
    Deliverable,
    ExecutionRunActivity,
    VerificationResult,
)

from ._helpers import (
    _build_timeline,
    _partial_deliverable,
    _question_text,
    _trigger_context,
)
from ._schemas import (
    RunDecision,
    RunDetailResponse,
    RunVerification,
)

router = APIRouter()


@router.get("/{run_id}/detail")
async def get_run_detail(
    run_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    runs: Annotated[RunRepository, Depends(get_run_repository)],
    decisions: Annotated[DecisionRepository, Depends(get_decision_repository)],
    deliverables: Annotated[DeliverableRepository, Depends(get_deliverable_repository)],
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
    run = await runs.get(run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")

    decision_rows = await decisions.list_by_run(run_id, workspace_id)

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

    # D6 — Deliverables for this run: the verified-final + any mid-loop partials.
    # We load all rows once and split by ``payload["kind"] == PARTIAL_DELIVERABLE_KIND``
    # so ``deliverable_id`` returns the verified-final regardless of timing (a
    # late-arriving partial must NOT shadow the terminal on the Run-view), and
    # ``partial_deliverables`` returns the streaming list (oldest-first, the
    # order they were emitted by the loop).
    deliverable_rows = await deliverables.list_by_run(run_id, workspace_id)
    partial_rows: list[Deliverable] = []
    final_rows: list[Deliverable] = []
    for row in deliverable_rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if payload.get("kind") == PARTIAL_DELIVERABLE_KIND:
            partial_rows.append(row)
        else:
            final_rows.append(row)
    # When multiple non-partial Deliverables exist (legacy / future), the
    # most-recent one wins — matches the prior "latest" semantics for non-partial
    # rows and keeps the verified terminal nondegenerate.
    final_row = final_rows[-1] if final_rows else None
    deliverable_id = final_row.id if final_row is not None else None
    deliverable_created_at = final_row.created_at if final_row is not None else None

    partial_deliverables = [_partial_deliverable(row) for row in partial_rows]

    # The run's STORY: meaningful activity rows, oldest-first.
    activities_stmt = (
        select(ExecutionRunActivity)
        .where(
            ExecutionRunActivity.run_id == run_id,
            ExecutionRunActivity.workspace_id == workspace_id,
        )
        .order_by(ExecutionRunActivity.created_at.asc())
    )
    activity_rows = list((await session.execute(activities_stmt)).scalars().all())
    activities, timeline_source = _build_timeline(
        activity_rows, verification_row, deliverable_id, deliverable_created_at
    )

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
        partial_deliverables=partial_deliverables,
        activities=activities,
        timeline_source=timeline_source,
    )


__all__ = ["router"]
