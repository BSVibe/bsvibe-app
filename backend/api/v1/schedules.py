"""/api/v1/schedules — author the schedules that let BSVibe start work on its own.

The producer surface of the ``workspace_schedules`` channel (S1). Before this
the ``ScheduleWorker`` polled that table in prod but nothing wrote rows to it —
a dead channel. These endpoints are the authoring input:

* ``POST   /api/v1/schedules``       — create a natural-language ``instruction``
  schedule (``{kind, text, cron_expr, product_id?, title?}``). The instruction
  IS the run task; the emitter carries ``text`` into the framer so a scheduled
  run frames the instruction (not "Untitled run").
* ``GET    /api/v1/schedules``       — list this workspace's schedules.
* ``DELETE /api/v1/schedules/{id}``  — remove a schedule.
* ``PATCH  /api/v1/schedules/{id}``  — enable / disable a schedule.

Workspace resolution mirrors the sibling routers (:mod:`backend.api.v1.notifications`):
the ``get_workspace_id`` dep publishes the workspace into the ORM-scoping
contextvar + Postgres RLS GUC. S1 is the ``instruction`` kind only; other kinds
(skill / product_tick / plugin_action) are S4.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.schedule.application.schedule_service import (
    ScheduleService,
    ScheduleValidationError,
)
from backend.schedule.infrastructure.schedule_db import (
    SCHEDULE_KIND_INSTRUCTION,
    WorkspaceScheduleRow,
)

router = APIRouter()


class ScheduleCreate(BaseModel):
    """Request body for authoring a schedule (S1: ``instruction`` kind only)."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(default=SCHEDULE_KIND_INSTRUCTION)
    text: str = Field(min_length=1, max_length=4000)
    cron_expr: str = Field(min_length=1, max_length=255)
    product_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=500)


class ScheduleEnabledPatch(BaseModel):
    """Request body for enabling / disabling a schedule."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ScheduleView(BaseModel):
    """Response shape for a schedule row."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    kind: str
    text: str
    cron_expr: str
    product_id: uuid.UUID | None
    title: str | None
    next_run_at: datetime
    last_fired_at: datetime | None
    enabled: bool


def _to_view(row: WorkspaceScheduleRow) -> ScheduleView:
    payload: dict[str, Any] = row.payload or {}
    text_value = payload.get("text")
    return ScheduleView(
        id=row.id,
        kind=row.kind,
        text=text_value if isinstance(text_value, str) else "",
        cron_expr=row.cron_expr,
        product_id=row.product_id,
        title=row.title,
        next_run_at=row.next_run_at,
        last_fired_at=row.last_fired_at,
        enabled=row.enabled,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ScheduleView,
    operation_id="create_schedule",
)
async def create_schedule(
    payload: ScheduleCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ScheduleView:
    """Author one schedule. 400 on an invalid cron expression / unsupported kind."""
    service = ScheduleService(session)
    try:
        row = await service.create(
            workspace_id=workspace_id,
            kind=payload.kind,
            text=payload.text,
            cron_expr=payload.cron_expr,
            product_id=payload.product_id,
            title=payload.title,
        )
    except ScheduleValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await session.commit()
    return _to_view(row)


@router.get("", response_model=list[ScheduleView], operation_id="list_schedules")
async def list_schedules(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ScheduleView]:
    """List this workspace's schedules, newest first."""
    service = ScheduleService(session)
    rows = await service.list(workspace_id=workspace_id)
    return [_to_view(row) for row in rows]


@router.delete(
    "/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_schedule",
)
async def delete_schedule(
    schedule_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """Delete a schedule. 404 if it does not exist in this workspace."""
    service = ScheduleService(session)
    deleted = await service.delete(schedule_id=schedule_id, workspace_id=workspace_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"schedule {schedule_id} not found"
        )
    await session.commit()


@router.patch(
    "/{schedule_id}",
    response_model=ScheduleView,
    operation_id="set_schedule_enabled",
)
async def set_schedule_enabled(
    schedule_id: uuid.UUID,
    payload: ScheduleEnabledPatch,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ScheduleView:
    """Enable or disable a schedule. 404 if it does not exist in this workspace."""
    service = ScheduleService(session)
    row = await service.set_enabled(
        schedule_id=schedule_id, workspace_id=workspace_id, enabled=payload.enabled
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"schedule {schedule_id} not found"
        )
    await session.commit()
    return _to_view(row)


__all__ = [
    "ScheduleCreate",
    "ScheduleEnabledPatch",
    "ScheduleView",
    "router",
]
