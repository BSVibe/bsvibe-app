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
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user_row, get_db_session
from backend.api.v1._identity_deps import (
    get_membership_repository,
    get_workspace_repository,
)
from backend.identity.db import MembershipRow, UserRow
from backend.identity.domain.repositories import (
    MembershipRepository,
    WorkspaceRepository,
)
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
    workspaces: WorkspaceRepository,
    memberships: MembershipRepository,
    user: UserRow,
    workspace_id: uuid.UUID,
) -> WorkspaceRow:
    """Return the live workspace iff the caller has an active membership, else 404.

    404 (not 403) so a non-member cannot probe which workspace ids exist.
    Soft-deleted workspaces (``deleted_at`` set) are treated as gone.
    """
    membership = await memberships.active_for_user_in_workspace(user.id, workspace_id)
    row = await workspaces.get_live(workspace_id) if membership is not None else None
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Workspace {workspace_id} not found"
        )
    return row


@router.get("")
async def list_workspaces(
    user: Annotated[UserRow, Depends(get_current_user_row)],
    workspaces: Annotated[WorkspaceRepository, Depends(get_workspace_repository)],
) -> list[WorkspaceResponse]:
    rows = await workspaces.list_for_user(user.id)
    return [WorkspaceResponse.model_validate(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate,
    user: Annotated[UserRow, Depends(get_current_user_row)],
    workspaces: Annotated[WorkspaceRepository, Depends(get_workspace_repository)],
    memberships: Annotated[MembershipRepository, Depends(get_membership_repository)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkspaceResponse:
    row = WorkspaceRow(
        id=uuid.uuid4(),
        name=payload.name,
        region=payload.region,
        safe_mode=payload.safe_mode,
    )
    await workspaces.add(row)
    await session.flush()
    await memberships.add(
        MembershipRow(id=uuid.uuid4(), user_id=user.id, workspace_id=row.id, role="owner")
    )
    await session.commit()
    return WorkspaceResponse.model_validate(row)


@router.get("/{workspace_id}")
async def get_workspace(
    workspace_id: uuid.UUID,
    user: Annotated[UserRow, Depends(get_current_user_row)],
    workspaces: Annotated[WorkspaceRepository, Depends(get_workspace_repository)],
    memberships: Annotated[MembershipRepository, Depends(get_membership_repository)],
) -> WorkspaceResponse:
    row = await _owned_workspace(workspaces, memberships, user, workspace_id)
    return WorkspaceResponse.model_validate(row)


@router.patch("/{workspace_id}")
async def update_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceUpdate,
    user: Annotated[UserRow, Depends(get_current_user_row)],
    workspaces: Annotated[WorkspaceRepository, Depends(get_workspace_repository)],
    memberships: Annotated[MembershipRepository, Depends(get_membership_repository)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkspaceResponse:
    row = await _owned_workspace(workspaces, memberships, user, workspace_id)
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
    workspaces: Annotated[WorkspaceRepository, Depends(get_workspace_repository)],
    memberships: Annotated[MembershipRepository, Depends(get_membership_repository)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    # Workflow §10.7 — soft delete: stamp deleted_at and end the caller's
    # membership. Row is retained for the 30-day window; the hard purge +
    # full cascade is a retention-infra follow-up.
    row = await _owned_workspace(workspaces, memberships, user, workspace_id)
    now = datetime.now(UTC)
    row.deleted_at = now
    membership = await memberships.active_for_user_in_workspace(user.id, workspace_id)
    if membership is not None:
        membership.left_at = now
    await session.commit()
