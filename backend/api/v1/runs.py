"""/api/v1/runs — read API for ExecutionRun rows.

Read-only on the HTTP surface; runs are *created* by the agent loop / workers
(Bundle G), never directly by an HTTP POST.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.execution.db import ExecutionRun, RunStatus

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
