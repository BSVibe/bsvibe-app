"""Schedule authoring tools — MCP parity for the schedule surface (S2).

Mirrors the S1 REST surface ``POST/GET/DELETE/PATCH /api/v1/schedules`` 1:1 over
MCP so the founder operating through Claude Code can author the schedules that
let BSVibe start work on its own — the same authoring input the PWA (S3) will
expose. The handlers are a thin transport over the SAME
:class:`~backend.schedule.application.schedule_service.ScheduleService` the REST
router uses: no duplicated cron validation, no second producer path. The create
emits through the INV-1 :data:`~backend.schedule.channels.WORKSPACE_SCHEDULES`
channel with the ``mcp:schedules_create`` producer id (declared alongside the
REST ``api:schedules_create``).

Scopes follow the established convention: ``mcp:read`` for the list,
``mcp:write`` for the mutations (create / delete / set_enabled). S2 covers the
``instruction`` kind only; richer kinds are S4. Input/output schemas mirror the
REST models with ``extra=forbid``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel

from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.schedule.application.schedule_service import (
    ScheduleService,
    ScheduleValidationError,
)
from backend.schedule.infrastructure.schedule_db import (
    SCHEDULE_KIND_INSTRUCTION,
    WorkspaceScheduleRow,
)

_MCP_PRODUCER_ID = "mcp:schedules_create"


# ---------------------------------------------------------------------------
# Schemas — mirror backend.api.v1.schedules 1:1 (extra=forbid).
# ---------------------------------------------------------------------------
class ScheduleCreateInput(BaseModel):
    """Author a schedule — mirrors REST ``ScheduleCreate`` (S2: ``instruction``)."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(default=SCHEDULE_KIND_INSTRUCTION)
    # Optional at the schema level: the ``instruction`` kind needs non-empty text
    # (enforced by ScheduleService) while ``product_tick`` ignores it — one schema
    # for both kinds, mirroring REST ``ScheduleCreate``.
    text: str = Field(default="", max_length=4000)
    cron_expr: str = Field(min_length=1, max_length=255)
    product_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=500)


class SchedulesListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduleDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_id: uuid.UUID


class ScheduleSetEnabledInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_id: uuid.UUID
    enabled: bool


class ScheduleView(BaseModel):
    """Response shape for a schedule row — mirrors REST ``ScheduleView``."""

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


class SchedulesListOutput(RootModel[list[ScheduleView]]):
    """List output — a JSON array of schedule views (mirrors REST list body)."""


class ScheduleDeleteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: bool
    schedule_id: str


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


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def _h_create(args: ScheduleCreateInput, ctx: ToolContext) -> Any:
    service = ScheduleService(ctx.session)
    try:
        row = await service.create(
            workspace_id=ctx.principal.workspace_id,
            kind=args.kind,
            text=args.text,
            cron_expr=args.cron_expr,
            product_id=args.product_id,
            title=args.title,
            producer_id=_MCP_PRODUCER_ID,
        )
    except ScheduleValidationError as exc:
        await ctx.session.rollback()
        raise ToolError(str(exc)) from exc
    await ctx.session.commit()
    return _to_view(row)


async def _h_list(_args: SchedulesListInput, ctx: ToolContext) -> Any:
    service = ScheduleService(ctx.session)
    rows = await service.list(workspace_id=ctx.principal.workspace_id)
    return SchedulesListOutput([_to_view(row) for row in rows])


async def _h_delete(args: ScheduleDeleteInput, ctx: ToolContext) -> Any:
    service = ScheduleService(ctx.session)
    deleted = await service.delete(
        schedule_id=args.schedule_id, workspace_id=ctx.principal.workspace_id
    )
    if not deleted:
        raise ToolError(f"schedule {args.schedule_id} not found")
    await ctx.session.commit()
    return ScheduleDeleteOutput(deleted=True, schedule_id=str(args.schedule_id))


async def _h_set_enabled(args: ScheduleSetEnabledInput, ctx: ToolContext) -> Any:
    service = ScheduleService(ctx.session)
    row = await service.set_enabled(
        schedule_id=args.schedule_id,
        workspace_id=ctx.principal.workspace_id,
        enabled=args.enabled,
    )
    if row is None:
        raise ToolError(f"schedule {args.schedule_id} not found")
    await ctx.session.commit()
    return _to_view(row)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_schedule_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_schedules_create",
            description=(
                "Author a schedule that lets BSVibe start a run on its own on a "
                "cron cadence. The `instruction` kind carries natural-language "
                "`text` that becomes the scheduled run's task. The `product_tick` "
                "kind sets only the cadence for a product (`product_id` required, "
                "`text` unused) — BSVibe decides the next action at fire time. "
                "400-equivalent error on an invalid cron expression or a "
                "product_tick with no product_id."
            ),
            input_schema=ScheduleCreateInput,
            output_schema=ScheduleView,
            handler=_h_create,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.schedules_create.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_schedules_list",
            description="List the active workspace's schedules, newest first.",
            input_schema=SchedulesListInput,
            output_schema=SchedulesListOutput,
            handler=_h_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_schedules_delete",
            description="Delete a schedule by id. Errors if it does not exist in this workspace.",
            input_schema=ScheduleDeleteInput,
            output_schema=ScheduleDeleteOutput,
            handler=_h_delete,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.schedules_delete.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_schedules_set_enabled",
            description=(
                "Enable or disable a schedule by id (a disabled schedule stops "
                "firing without being deleted). Errors if it does not exist."
            ),
            input_schema=ScheduleSetEnabledInput,
            output_schema=ScheduleView,
            handler=_h_set_enabled,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.schedules_set_enabled.invoked",
        )
    )


__all__ = ["register_schedule_tools"]
