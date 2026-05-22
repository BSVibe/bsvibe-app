"""/api/v1/workspaces — the caller's own workspaces (Workflow §3).

Unlike the other v1 routers (which operate within a single resolved
``current_workspace_id``), this router lets a principal see *all* workspaces
they belong to. It is the one legitimate place a ``workspace_id`` path param
appears — every operation is gated on the caller having an active
``Membership`` in that workspace. Creating a workspace grants the creator an
``owner`` membership.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user_row, get_db_session
from backend.identity.db import MembershipRow, UserRow
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


async def _owned_workspace(
    session: AsyncSession, user: UserRow, workspace_id: uuid.UUID
) -> WorkspaceRow:
    """Return the workspace iff the caller has an active membership, else 404.

    404 (not 403) so a non-member cannot probe which workspace ids exist.
    """
    membership = (
        (
            await session.execute(
                select(MembershipRow).where(
                    MembershipRow.user_id == user.id,
                    MembershipRow.workspace_id == workspace_id,
                    MembershipRow.left_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    row = await session.get(WorkspaceRow, workspace_id) if membership is not None else None
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Workspace {workspace_id} not found"
        )
    return row


@router.get("")
async def list_workspaces(
    user: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[WorkspaceResponse]:
    rows = (
        (
            await session.execute(
                select(WorkspaceRow)
                .join(MembershipRow, MembershipRow.workspace_id == WorkspaceRow.id)
                .where(MembershipRow.user_id == user.id, MembershipRow.left_at.is_(None))
                .order_by(WorkspaceRow.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [WorkspaceResponse.model_validate(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate,
    user: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkspaceResponse:
    row = WorkspaceRow(
        id=uuid.uuid4(),
        name=payload.name,
        region=payload.region,
        safe_mode=payload.safe_mode,
    )
    session.add(row)
    await session.flush()
    session.add(MembershipRow(id=uuid.uuid4(), user_id=user.id, workspace_id=row.id, role="owner"))
    await session.commit()
    return WorkspaceResponse.model_validate(row)


@router.get("/{workspace_id}")
async def get_workspace(
    workspace_id: uuid.UUID,
    user: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkspaceResponse:
    row = await _owned_workspace(session, user, workspace_id)
    return WorkspaceResponse.model_validate(row)


@router.patch("/{workspace_id}")
async def update_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceUpdate,
    user: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkspaceResponse:
    row = await _owned_workspace(session, user, workspace_id)
    for field in ("name", "region", "safe_mode"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)
    await session.commit()
    return WorkspaceResponse.model_validate(row)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: uuid.UUID,
    user: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    row = await _owned_workspace(session, user, workspace_id)
    await session.delete(row)
    await session.commit()
