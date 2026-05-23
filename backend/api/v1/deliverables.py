"""/api/v1/deliverables — read API for Deliverable rows.

Read-only on the HTTP surface; deliverables are *produced* by the agent loop /
workers on a verified run (Bundle G), never directly by an HTTP POST. The PWA
Brief's "recently shipped" reads this to surface real artifacts.

The ``payload`` column is free-form JSON written by the orchestrator and shaped
``{summary, artifact_refs}``; we map it defensively (missing/odd values degrade
to ``None`` / ``[]``) so a malformed row never 500s the response model.
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
from backend.execution.db import Deliverable, DeliverableType

router = APIRouter()


class DeliverableResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    deliverable_type: DeliverableType
    summary: str | None = None
    artifact_refs: list[str] = []
    artifact_uri: str | None = None
    created_at: datetime


def _summary_of(payload: dict[str, Any]) -> str | None:
    """Pull a string ``summary`` out of the free-form payload, else ``None``."""
    value = payload.get("summary")
    return value if isinstance(value, str) else None


def _artifact_refs_of(payload: dict[str, Any]) -> list[str]:
    """Pull a list of string ``artifact_refs`` out of the payload, else ``[]``."""
    value = payload.get("artifact_refs")
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _to_response(row: Deliverable) -> DeliverableResponse:
    payload = row.payload if isinstance(row.payload, dict) else {}
    return DeliverableResponse(
        id=row.id,
        run_id=row.run_id,
        workspace_id=row.workspace_id,
        deliverable_type=row.deliverable_type,
        summary=_summary_of(payload),
        artifact_refs=_artifact_refs_of(payload),
        artifact_uri=row.artifact_uri,
        created_at=row.created_at,
    )


@router.get("")
async def list_deliverables(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    run_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[DeliverableResponse]:
    """List recent Deliverable rows for the workspace, newest first.

    Optional ``run_id`` narrows to one run's deliverables.
    """
    limit = max(1, min(limit, 200))
    stmt = select(Deliverable).where(Deliverable.workspace_id == workspace_id)
    if run_id is not None:
        stmt = stmt.where(Deliverable.run_id == run_id)
    stmt = stmt.order_by(Deliverable.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return [_to_response(row) for row in result.scalars().all()]


@router.get("/{deliverable_id}")
async def get_deliverable(
    deliverable_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DeliverableResponse:
    """Fetch one Deliverable by id, scoped to the caller's workspace."""
    row = await session.get(Deliverable, deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )
    return _to_response(row)
