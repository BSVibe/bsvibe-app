"""Notification preference tools — UI-parity setup surface (Lift D3a).

Mirrors the PWA's Settings → Notifications tab + the REST
``GET / PUT /api/v1/notifications/prefs`` endpoints. The matrix is the
events x channels enable grid (validated against the known events and
channels) plus a quiet-hours window. Get is a no-op create when the row
is missing — the workspace reads
:data:`backend.notifications.db.DEFAULT_MATRIX` defaults, then those
defaults persist.

No ``test_notification`` tool is shipped — neither the PWA nor the
backend exposes a notification-send test today (v1 stores preferences
only; the actual email / Slack delivery wiring is a later phase). When
that ships, this surface gains a matching ``bsvibe_notifications_test``
tool — until then, MCP mirrors the PWA exactly.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.mcp.api import Tool, ToolContext, ToolRegistry
from backend.notifications.db import (
    DEFAULT_CHANNELS,
    DEFAULT_EVENTS,
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
    NotificationPrefsRow,
    default_matrix,
)

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EVENT_SET = frozenset(DEFAULT_EVENTS)
_CHANNEL_SET = frozenset(DEFAULT_CHANNELS)


def _validate_matrix(matrix: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
    """Same validator the REST surface applies — reject unknown event/channel ids."""
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


class _PrefsBody(BaseModel):
    """Shared request/response shape — mirrors REST ``PrefsBody`` 1:1."""

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


def _row_to_body(row: NotificationPrefsRow) -> _PrefsBody:
    return _PrefsBody(
        matrix=row.matrix,
        quiet_hours_enabled=row.quiet_hours_enabled,
        quiet_hours_start=row.quiet_hours_start,
        quiet_hours_end=row.quiet_hours_end,
    )


# ---------------------------------------------------------------------------
# bsvibe_notification_prefs_get
# ---------------------------------------------------------------------------
class NotificationPrefsGetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _h_get(_args: NotificationPrefsGetInput, ctx: ToolContext) -> Any:
    row = await _get_or_create(ctx.session, ctx.principal.workspace_id)
    return _row_to_body(row)


# ---------------------------------------------------------------------------
# bsvibe_notification_prefs_update — replace matrix + quiet hours wholesale
# ---------------------------------------------------------------------------
class NotificationPrefsUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matrix: dict[str, dict[str, bool]] = Field(
        ..., description="Full events × channels enable matrix — replaces wholesale."
    )
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


async def _h_update(args: NotificationPrefsUpdateInput, ctx: ToolContext) -> Any:
    row = await _get_or_create(ctx.session, ctx.principal.workspace_id)
    row.matrix = args.matrix
    row.quiet_hours_enabled = args.quiet_hours_enabled
    row.quiet_hours_start = args.quiet_hours_start
    row.quiet_hours_end = args.quiet_hours_end
    await ctx.session.commit()
    await ctx.session.refresh(row)
    return _row_to_body(row)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_notifications_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_notification_prefs_get",
            description=(
                "Get the active workspace's notification preferences "
                "(events × channels matrix + quiet-hours window). A fresh "
                "workspace reads sensible defaults, which are then persisted."
            ),
            input_schema=NotificationPrefsGetInput,
            output_schema=_PrefsBody,
            handler=_h_get,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_notification_prefs_update",
            description=(
                "Replace the notification matrix + quiet hours wholesale for "
                "the active workspace. The matrix must list exactly the known "
                "events and channels."
            ),
            input_schema=NotificationPrefsUpdateInput,
            output_schema=_PrefsBody,
            handler=_h_update,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.notification_prefs_update.invoked",
        )
    )


__all__ = ["register_notifications_tools"]
