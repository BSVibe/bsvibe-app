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
from backend.notifications.bindings import available_channels
from backend.notifications.db import (
    DEFAULT_EVENTS,
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
    NotificationPrefsRow,
    default_matrix,
)

router = APIRouter()

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EVENT_SET = frozenset(DEFAULT_EVENTS)


def _validate_matrix(matrix: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
    """Validate the matrix: exactly the known events; any channel keys tolerated.

    Events are still the fixed :data:`DEFAULT_EVENTS`. Channels are NOT fixed —
    they are derived per workspace from connector bindings, so the validator
    accepts any subset of channel keys (a stale key for a since-removed connector
    is harmless — ignored at send time — rather than rejected). Values must be
    booleans.
    """
    if set(matrix.keys()) != _EVENT_SET:
        raise ValueError(
            f"matrix events must be exactly {sorted(_EVENT_SET)}; got {sorted(matrix.keys())}"
        )
    for event_id, channels in matrix.items():
        for channel_id, enabled in channels.items():
            if not isinstance(enabled, bool):
                raise ValueError(
                    f"matrix[{event_id!r}][{channel_id!r}] must be a bool; got {enabled!r}"
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


class PrefsView(PrefsBody):
    """GET/PUT response — the stored prefs plus the derived channel columns.

    ``available_channels`` is the workspace's live notification channels
    (``in_app`` + every bound notify-channel connector), recomputed at read time
    from connector bindings. It is response-only: the PWA renders the matrix
    columns from it, and it is not settable (a PUT that echoes it back is
    rejected by ``extra=forbid`` on :class:`PrefsBody`).
    """

    available_channels: list[str]


def _to_view(row: NotificationPrefsRow, channels: list[str]) -> PrefsView:
    return PrefsView(
        matrix=row.matrix,
        quiet_hours_enabled=row.quiet_hours_enabled,
        quiet_hours_start=row.quiet_hours_start,
        quiet_hours_end=row.quiet_hours_end,
        available_channels=channels,
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
