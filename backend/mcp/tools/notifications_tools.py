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

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.mcp.api import Tool, ToolContext, ToolRegistry
from backend.notifications.bindings import available_channels
from backend.notifications.serialization import (
    HHMM as _HHMM,
)
from backend.notifications.serialization import (
    PrefsView as _PrefsView,
)
from backend.notifications.serialization import (
    get_or_create_prefs as _get_or_create,
)
from backend.notifications.serialization import (
    prefs_view_from_row as _row_to_view,
)
from backend.notifications.serialization import (
    validate_matrix as _validate_matrix,
)


# ---------------------------------------------------------------------------
# bsvibe_notification_prefs_get
# ---------------------------------------------------------------------------
class NotificationPrefsGetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _h_get(_args: NotificationPrefsGetInput, ctx: ToolContext) -> Any:
    row = await _get_or_create(ctx.session, ctx.principal.workspace_id)
    channels = await available_channels(ctx.session, workspace_id=ctx.principal.workspace_id)
    return _row_to_view(row, channels)


# ---------------------------------------------------------------------------
# bsvibe_notification_prefs_update — replace matrix + quiet hours wholesale
# ---------------------------------------------------------------------------
class NotificationPrefsUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matrix: dict[str, bool] = Field(
        ..., description="Full events × channels enable matrix — replaces wholesale."
    )
    quiet_hours_enabled: bool
    quiet_hours_start: str
    quiet_hours_end: str

    @field_validator("matrix")
    @classmethod
    def _check_matrix(cls, v: dict[str, bool]) -> dict[str, bool]:
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
    channels = await available_channels(ctx.session, workspace_id=ctx.principal.workspace_id)
    return _row_to_view(row, channels)


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
            output_schema=_PrefsView,
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
                "events; channel columns are derived per workspace from its "
                "connector bindings (see available_channels on the get output)."
            ),
            input_schema=NotificationPrefsUpdateInput,
            output_schema=_PrefsView,
            handler=_h_update,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.notification_prefs_update.invoked",
        )
    )


__all__ = ["register_notifications_tools"]
