"""``/api/v1/workspace`` — read + rename the caller's workspace.

Sits alongside :mod:`backend.api.v1.workspace_compliance` under the singular
``/workspace`` prefix. Where the compliance routes export and document the
workspace (Art. 15 / 20 / 30), these routes are the everyday founder-facing
surface: "what's my workspace called, and let me rename it."

Two endpoints:

* ``GET    /api/v1/workspace`` — returns the active workspace's id + name.
  Drives Settings → General's "Workspace name" field so it no longer falls
  back to the founder's email when no real name is stored
  (the /impeccable audit's Lift 13 finding).
* ``PATCH  /api/v1/workspace`` — accepts ``{ name }`` and stores it on the
  active workspace's row. ``extra="forbid"`` rejects unknown fields.

Workspace resolution + RLS guard fire automatically via
``Depends(get_workspace_id)`` exactly the same way the compliance routes
engage them.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.workspaces.db import WorkspaceRow

router = APIRouter()


class WorkspaceOut(BaseModel):
    """GET response — the basic workspace facts the PWA surfaces."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str


class WorkspaceUpdate(BaseModel):
    """PATCH body — only the editable name field for now. The schema is the
    extension seam: adding a future-editable field (e.g. avatar_url, locale
    default) ships as another optional field here rather than another
    endpoint."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)


@router.get("", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkspaceOut:
    """Return the active workspace's id + name."""
    workspace = (
        await session.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
    ).scalar_one_or_none()
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return WorkspaceOut(id=workspace.id, name=workspace.name)


@router.patch("", response_model=WorkspaceOut)
async def update_workspace(
    payload: WorkspaceUpdate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkspaceOut:
    """Update the workspace name on the active workspace.

    The workspace_id contextvar is what selects the row — the caller cannot
    write a different workspace's row, defense-in-depth from the RLS GUC on
    the same connection.
    """
    workspace = (
        await session.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
    ).scalar_one_or_none()
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    workspace.name = payload.name.strip()
    await session.commit()
    return WorkspaceOut(id=workspace.id, name=workspace.name)


__all__ = ["router"]
