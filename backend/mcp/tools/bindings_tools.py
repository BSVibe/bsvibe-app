"""Resource binding tools — UI-parity workflow surface (Lift D3b).

Wraps the per-Product × ConnectorAccount 3-knob binding surface that the
PWA's ``ProductBindings`` component drives via
``/api/v1/products/{product_id}/bindings``. Handlers are thin: they
re-use :class:`SqlAlchemyResourceBindingRepository` (the same repo the
REST endpoints call) so the MCP and PWA paths land on one canonical
mutation chain. Every read / write is workspace-scoped via the
principal's ``workspace_id``; a product / binding belonging to another
workspace simply isn't there → ``ToolError`` (the MCP analogue of REST's
404, the same shape the connector tools return).

Scopes follow the existing convention: ``mcp:read`` for list,
``mcp:write`` for create / update / delete.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel
from sqlalchemy import select

from backend.connectors.db import ConnectorAccountRow
from backend.identity.infrastructure.repositories import (
    SqlAlchemyResourceBindingRepository,
)
from backend.identity.workspaces_db import ProductRow, ResourceBindingRow
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry


class _Envelope(RootModel[Any]):
    """Permissive output envelope — preserves the natural JSON shape."""


_VALID_OUTPUT_MODES = ("safe", "direct")


def _row_to_dict(row: ResourceBindingRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "product_id": str(row.product_id),
        "connector_account_id": str(row.connector_account_id),
        "resource_id": row.resource_id,
        "selection": row.selection,
        "trigger": row.trigger,
        "output_mode": row.output_mode,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _resolve_product(ctx: ToolContext, product_id: uuid.UUID) -> ProductRow:
    product = await ctx.session.get(ProductRow, product_id)
    if product is None or product.workspace_id != ctx.principal.workspace_id:
        raise ToolError(f"product not found: {product_id}")
    return product


async def _resolve_connector_account(
    ctx: ToolContext, connector_account_id: uuid.UUID
) -> ConnectorAccountRow:
    row = await ctx.session.get(ConnectorAccountRow, connector_account_id)
    if row is None or row.workspace_id != ctx.principal.workspace_id:
        raise ToolError(f"connector_account not found: {connector_account_id}")
    return row


# ---------------------------------------------------------------------------
# bsvibe_bindings_list
# ---------------------------------------------------------------------------
class BindingsListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: uuid.UUID | None = None


async def _h_list(args: BindingsListInput, ctx: ToolContext) -> Any:
    repo = SqlAlchemyResourceBindingRepository(ctx.session)
    if args.product_id is not None:
        await _resolve_product(ctx, args.product_id)
        rows = await repo.list_for_product(
            workspace_id=ctx.principal.workspace_id,
            product_id=args.product_id,
        )
    else:
        # Workspace-wide list — no PWA route surfaces this today, but it is
        # the natural MCP affordance ("show me every binding in the active
        # workspace"). Falls through a direct query to keep the repo
        # surface minimal.
        stmt = (
            select(ResourceBindingRow)
            .where(ResourceBindingRow.workspace_id == ctx.principal.workspace_id)
            .order_by(ResourceBindingRow.created_at.asc())
        )
        rows = (await ctx.session.execute(stmt)).scalars().all()
    return _Envelope([_row_to_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# bsvibe_bindings_create
# ---------------------------------------------------------------------------
class BindingsCreateInput(BaseModel):
    """Mirror of :class:`ResourceBindingCreate` (the REST schema).

    The PWA's ``ProductBindings`` form posts the same field set; this tool
    accepts it 1:1 plus the parent ``product_id`` (a path param in REST).
    """

    model_config = ConfigDict(extra="forbid")

    product_id: uuid.UUID
    connector_account_id: uuid.UUID
    resource_id: str = Field(min_length=1, max_length=512)
    selection: dict[str, Any] = Field(default_factory=dict)
    trigger: dict[str, Any] | None = None
    output_mode: str = "safe"


async def _h_create(args: BindingsCreateInput, ctx: ToolContext) -> Any:
    if args.output_mode not in _VALID_OUTPUT_MODES:
        raise ToolError(
            f"output_mode must be one of {_VALID_OUTPUT_MODES}, got {args.output_mode!r}"
        )
    await _resolve_product(ctx, args.product_id)
    await _resolve_connector_account(ctx, args.connector_account_id)
    repo = SqlAlchemyResourceBindingRepository(ctx.session)
    row = await repo.create(
        workspace_id=ctx.principal.workspace_id,
        product_id=args.product_id,
        connector_account_id=args.connector_account_id,
        resource_id=args.resource_id,
        selection=args.selection,
        trigger=args.trigger,
        output_mode=args.output_mode,
    )
    await ctx.session.commit()
    await ctx.session.refresh(row)
    return _Envelope(_row_to_dict(row))


# ---------------------------------------------------------------------------
# bsvibe_bindings_update
# ---------------------------------------------------------------------------
class BindingsUpdateInput(BaseModel):
    """Patch a binding's knobs. ``None`` fields are left as-is."""

    model_config = ConfigDict(extra="forbid")

    binding_id: uuid.UUID
    selection: dict[str, Any] | None = None
    trigger: dict[str, Any] | None = None
    output_mode: str | None = None


async def _h_update(args: BindingsUpdateInput, ctx: ToolContext) -> Any:
    if args.output_mode is not None and args.output_mode not in _VALID_OUTPUT_MODES:
        raise ToolError(
            f"output_mode must be one of {_VALID_OUTPUT_MODES}, got {args.output_mode!r}"
        )
    repo = SqlAlchemyResourceBindingRepository(ctx.session)
    row = await repo.get(workspace_id=ctx.principal.workspace_id, binding_id=args.binding_id)
    if row is None:
        raise ToolError(f"binding not found: {args.binding_id}")
    await repo.update(
        row,
        selection=args.selection,
        trigger=args.trigger,
        output_mode=args.output_mode,
    )
    await ctx.session.commit()
    await ctx.session.refresh(row)
    return _Envelope(_row_to_dict(row))


# ---------------------------------------------------------------------------
# bsvibe_bindings_delete
# ---------------------------------------------------------------------------
class BindingsDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_id: uuid.UUID


class BindingsDeleteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deleted: bool
    binding_id: str


async def _h_delete(args: BindingsDeleteInput, ctx: ToolContext) -> Any:
    repo = SqlAlchemyResourceBindingRepository(ctx.session)
    deleted = await repo.delete(workspace_id=ctx.principal.workspace_id, binding_id=args.binding_id)
    if not deleted:
        raise ToolError(f"binding not found: {args.binding_id}")
    await ctx.session.commit()
    return BindingsDeleteOutput(deleted=True, binding_id=str(args.binding_id))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_bindings_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_bindings_list",
            description=(
                "List per-Product × ConnectorAccount resource bindings in the "
                "active workspace. Pass `product_id` to scope to one product; "
                "omit for a workspace-wide listing."
            ),
            input_schema=BindingsListInput,
            output_schema=_Envelope,
            handler=_h_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_bindings_create",
            description=(
                "Bind a connector resource to a Product. Mirrors the PWA's "
                "Add-binding form: selection (connector-shaped scope), trigger "
                "({enabled, filters}), and output_mode ('safe' queues the "
                "deliverable for founder approval, 'direct' auto-delivers)."
            ),
            input_schema=BindingsCreateInput,
            output_schema=_Envelope,
            handler=_h_create,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.bindings_create.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_bindings_update",
            description=(
                "Patch a binding's knobs (selection / trigger / output_mode). "
                "Pass only the fields you want to change — `None` leaves the "
                "rest as-is. Dict knobs are REPLACED, not merged."
            ),
            input_schema=BindingsUpdateInput,
            output_schema=_Envelope,
            handler=_h_update,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.bindings_update.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_bindings_delete",
            description="Hard-delete a binding by id.",
            input_schema=BindingsDeleteInput,
            output_schema=BindingsDeleteOutput,
            handler=_h_delete,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.bindings_delete.invoked",
        )
    )


__all__ = ["register_bindings_tools"]
