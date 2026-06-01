"""Read-only deliverable browse endpoints (Lift §17.9 sub-file).

* ``GET /api/v1/deliverables`` — list recent Deliverable rows for the
  caller's workspace (optionally narrowed to one ``run_id``).
* ``GET /api/v1/deliverables/{deliverable_id}`` — fetch one by id.

Both are thin adapters (D35): parse → DeliverableRepository → serialize.
Per-page B4 "verified" flag derives from a single PASSED-verification lookup
(:func:`_helpers.verified_run_ids`), never from Deliverable existence.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.v1._workflow_deps import get_deliverable_repository
from backend.workflow.domain.repositories import DeliverableRepository

from ._helpers import run_is_verified, verified_run_ids
from ._schemas import DeliverableResponse, to_response

router = APIRouter()


@router.get("")
async def list_deliverables(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    deliverables: Annotated[DeliverableRepository, Depends(get_deliverable_repository)],
    run_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[DeliverableResponse]:
    """List recent Deliverable rows for the workspace, newest first.

    Optional ``run_id`` narrows to one run's deliverables.
    """
    limit = max(1, min(limit, 200))
    rows = await deliverables.list_by_workspace(workspace_id, run_id=run_id, limit=limit)
    verified = await verified_run_ids(session, workspace_id, {row.run_id for row in rows})
    return [to_response(row, verified=row.run_id in verified) for row in rows]


@router.get("/{deliverable_id}")
async def get_deliverable(
    deliverable_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    deliverables: Annotated[DeliverableRepository, Depends(get_deliverable_repository)],
) -> DeliverableResponse:
    """Fetch one Deliverable by id, scoped to the caller's workspace."""
    row = await deliverables.get(deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )
    verified = await run_is_verified(session, workspace_id, row.run_id)
    return to_response(row, verified=verified)
