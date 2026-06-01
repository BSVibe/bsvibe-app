"""Read endpoints for ``/api/v1/runs`` — list + single-row fetch.

Strictly read-only adapters (D35 thin-adapter): parse the workspace + repo
out of DI, hand off to :class:`RunRepository`, serialize the rows. Mutations
are intentionally absent here — runs are created by the agent loop / workers
(Bundle G), never by an HTTP POST. The richer per-run inspection surface
lives in :mod:`.detail`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from backend.api.deps import get_workspace_id
from backend.api.v1._workflow_deps import get_run_repository
from backend.workflow.domain.repositories import RunRepository

from ._helpers import _intent_of
from ._schemas import RunResponse

router = APIRouter()


@router.get("")
async def list_runs(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    runs: Annotated[RunRepository, Depends(get_run_repository)],
    limit: int = 50,
) -> list[RunResponse]:
    """List recent ExecutionRun rows for the workspace, newest first."""
    limit = max(1, min(limit, 200))
    rows = await runs.list_by_workspace(workspace_id, limit=limit)
    return [
        RunResponse(
            id=row.id,
            workspace_id=row.workspace_id,
            product_id=row.product_id,
            request_id=row.request_id,
            status=row.status,
            intent=_intent_of(row.payload),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


@router.get("/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    runs: Annotated[RunRepository, Depends(get_run_repository)],
) -> RunResponse:
    """Fetch one ExecutionRun by id, scoped to the caller's workspace."""
    row = await runs.get(run_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    return RunResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        product_id=row.product_id,
        request_id=row.request_id,
        status=row.status,
        intent=_intent_of(row.payload),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


__all__ = ["router"]
