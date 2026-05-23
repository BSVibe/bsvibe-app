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

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.notifications.db import (
    DEFAULT_CHANNELS,
    DEFAULT_EVENTS,
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
    NotificationPrefsRow,
    default_matrix,
)

router = APIRouter()

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EVENT_SET = frozenset(DEFAULT_EVENTS)
_CHANNEL_SET = frozenset(DEFAULT_CHANNELS)


def _validate_matrix(matrix: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
    """Reject unknown event/channel ids; require the full known grid."""
    if set(matrix.keys()) != _EVENT_SET:
        raise ValueError(
            f"matrix events must be exactly {sorted(_EVENT_SET)}; got {sorted(matrix.keys())}"
        )
    for event_id, channels in matrix.items():
        if set(channels.keys()) != _CHANNEL_SET:
            raise ValueError(
                f"matrix[{event_id!r}] channels must be exactly {sorted(_CHANNEL_SET)}"
            )
    return matrix


class PrefsBody(BaseModel):
    """Shared request/response shape for the prefs surface."""

    model_config = ConfigDict(extra="forbid")

    matrix: dict[str, dict[str, bool]]
    quiet_hours_enabled: bool
    quiet_hours_start: str
    quiet_hours_end: str

    @field_validator("matrix")
    @classmethod
    def _check_matrix(cls, v: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
        return _validate_matrix(v)

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def _check_time(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError(f"quiet-hours time must be HH:MM (00:00-23:59); got {v!r}")
        return v


def _to_body(row: NotificationPrefsRow) -> PrefsBody:
    return PrefsBody(
        matrix=row.matrix,
        quiet_hours_enabled=row.quiet_hours_enabled,
        quiet_hours_start=row.quiet_hours_start,
        quiet_hours_end=row.quiet_hours_end,
    )


async def _get_or_create(session: AsyncSession, workspace_id: uuid.UUID) -> NotificationPrefsRow:
    row = (
        await session.execute(
            select(NotificationPrefsRow).where(NotificationPrefsRow.workspace_id == workspace_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = NotificationPrefsRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            matrix=default_matrix(),
            quiet_hours_enabled=False,
            quiet_hours_start=DEFAULT_QUIET_HOURS_START,
            quiet_hours_end=DEFAULT_QUIET_HOURS_END,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


@router.get("/prefs")
async def get_prefs(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PrefsBody:
    """Get-or-create the active workspace's notification preferences."""
    row = await _get_or_create(session, workspace_id)
    return _to_body(row)


@router.put("/prefs")
async def put_prefs(
    payload: PrefsBody,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PrefsBody:
    """Replace the matrix + quiet hours wholesale (single row per workspace)."""
    row = await _get_or_create(session, workspace_id)
    row.matrix = payload.matrix
    row.quiet_hours_enabled = payload.quiet_hours_enabled
    row.quiet_hours_start = payload.quiet_hours_start
    row.quiet_hours_end = payload.quiet_hours_end
    await session.commit()
    await session.refresh(row)
    return _to_body(row)


__all__ = ["PrefsBody", "get_prefs", "put_prefs", "router"]
