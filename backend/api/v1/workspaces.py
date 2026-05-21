"""/api/v1/workspaces — CRUD for the top-level Workspace entity (Workflow §3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session
from backend.workspaces.db import WorkspaceRow

router = APIRouter()


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    region: str = Field(default="us-1", max_length=32)
    safe_mode: bool = True


class WorkspaceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=32)
    safe_mode: bool | None = None


class WorkspaceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    name: str
    region: str
    safe_mode: bool
    created_at: datetime
    updated_at: datetime


@router.get("")
async def list_workspaces(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[WorkspaceResponse]:
    rows = (
        (await session.execute(select(WorkspaceRow).order_by(WorkspaceRow.created_at.desc())))
        .scalars()
        .all()
    )
    return [WorkspaceResponse.model_validate(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkspaceResponse:
    row = WorkspaceRow(
        id=uuid.uuid4(),
        name=payload.name,
        region=payload.region,
        safe_mode=payload.safe_mode,
    )
    session.add(row)
    await session.commit()
    return WorkspaceResponse.model_validate(row)


@router.get("/{workspace_id}")
async def get_workspace(
    workspace_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkspaceResponse:
    row = await session.get(WorkspaceRow, workspace_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Workspace {workspace_id} not found"
        )
    return WorkspaceResponse.model_validate(row)


@router.patch("/{workspace_id}")
async def update_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkspaceResponse:
    row = await session.get(WorkspaceRow, workspace_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Workspace {workspace_id} not found"
        )
    for field in ("name", "region", "safe_mode"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)
    await session.commit()
    return WorkspaceResponse.model_validate(row)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    row = await session.get(WorkspaceRow, workspace_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Workspace {workspace_id} not found"
        )
    await session.delete(row)
    await session.commit()
