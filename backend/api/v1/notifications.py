"""/api/v1/notifications/prefs — workspace notification preferences.

The founder's Settings -> Notifications surface. v1 stores the PREFERENCES
only (an events x channels enable matrix + a quiet-hours window); the actual
email / Slack delivery wiring is a later phase.

* ``GET  /api/v1/notifications/prefs`` — get-or-create for the active
  workspace. A workspace with no row yet reads the sensible defaults
  (:data:`backend.notifications.db.DEFAULT_MATRIX`), which are then persisted,
  so a later PUT updates a real row.
* ``PUT  /api/v1/notifications/prefs`` — replace the matrix + quiet hours
  wholesale. The matrix is validated against the known event/channel ids and
  quiet-hours times must be ``HH:MM``.

Workspace resolution mirrors :mod:`backend.api.v1.runs` (the ``get_workspace_id``
dep publishes the workspace into the ORM-scoping contextvar). Per-product
overrides from the design are intentionally OMITTED in v1 — this is the global
matrix + quiet hours only.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.notifications.bindings import available_channels
from backend.notifications.serialization import (
    PrefsBody,
    PrefsView,
    get_or_create_prefs,
    prefs_view_from_row,
)

router = APIRouter()

_to_view = prefs_view_from_row


_get_or_create = get_or_create_prefs


@router.get("/prefs")
async def get_prefs(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PrefsView:
    """Get-or-create the active workspace's notification preferences."""
    row = await _get_or_create(session, workspace_id)
    channels = await available_channels(session, workspace_id=workspace_id)
    return _to_view(row, channels)


@router.put("/prefs")
async def put_prefs(
    payload: PrefsBody,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PrefsView:
    """Replace the matrix + quiet hours wholesale (single row per workspace)."""
    row = await _get_or_create(session, workspace_id)
    row.matrix = payload.matrix
    row.quiet_hours_enabled = payload.quiet_hours_enabled
    row.quiet_hours_start = payload.quiet_hours_start
    row.quiet_hours_end = payload.quiet_hours_end
    await session.commit()
    await session.refresh(row)
    channels = await available_channels(session, workspace_id=workspace_id)
    return _to_view(row, channels)


__all__ = ["PrefsBody", "PrefsView", "get_prefs", "put_prefs", "router"]
