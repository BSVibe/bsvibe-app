"""``GET /api/v1/runs/{run_id}/detail`` — the inspectable run-detail surface.

A D35 thin adapter: pull the workspace + repositories out of DI, hand off to
:func:`backend.workflow.application.run_detail.build_run_detail`, spell
"unknown here" as a 404. The derivation itself lives in the Workflow context
so the ``bsvibe_runs_detail`` MCP tool runs the same one.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.v1._workflow_deps import (
    get_decision_repository,
    get_deliverable_repository,
    get_run_repository,
)
from backend.workflow.application.run_detail import build_run_detail
from backend.workflow.domain.repositories import (
    DecisionRepository,
    DeliverableRepository,
    RunRepository,
)
from backend.workflow.serialization.run_views import RunDetailResponse

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

    A cross-workspace / unknown id is 404, never a leak.
    """
    detail = await build_run_detail(
        run_id=run_id,
        workspace_id=workspace_id,
        session=session,
        runs=runs,
        decisions=decisions,
        deliverables=deliverables,
    )
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    return detail


__all__ = ["router"]
