"""Safe Mode tools — the founder's recovery path over MCP.

Three tools: list pending, approve, deny. These are the highest-value
MCP surface for the founder's dogfood loop — a stalled run posts a
queued delivery into Safe Mode, the founder approves (or denies) via
their MCP-connected client, the run resumes.

Approve maps onto :func:`backend.workflow.infrastructure.workers.delivery_worker.dispatch_delivery`
so the MCP path and the PWA path land on the SAME outbound code path.
Deny just flips the queue row and emits no dispatch.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.workers.delivery_worker import (
    dispatch_delivery,
    persist_compensation_handles,
)

logger = structlog.get_logger(__name__)


class _Output(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# bsvibe_safe_mode_list_pending
# ---------------------------------------------------------------------------
class SafeModeListPendingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID | None = None


class SafeModeItem(_Output):
    id: str
    deliverable_id: str
    run_id: str | None = None
    status: str
    expires_at: str | None = None
    extension_count: int = 0
    created_at: str | None = None


class SafeModeListPendingOutput(_Output):
    total: int
    items: list[SafeModeItem]


def _item_to_dict(item: Any) -> SafeModeItem:
    return SafeModeItem(
        id=str(item.id),
        deliverable_id=str(item.deliverable_id),
        run_id=str(item.run_id) if item.run_id else None,
        status=getattr(item.status, "value", str(item.status)),
        expires_at=item.expires_at.isoformat() if item.expires_at else None,
        extension_count=item.extension_count,
        created_at=item.created_at.isoformat() if item.created_at else None,
    )


async def _h_list_pending(args: SafeModeListPendingInput, ctx: ToolContext) -> Any:
    queue = SafeModeQueue(ctx.session)
    if args.run_id is not None:
        items = await queue.list_pending_for_run(
            workspace_id=ctx.principal.workspace_id, run_id=args.run_id
        )
    else:
        items = await queue.list_pending(workspace_id=ctx.principal.workspace_id)
    return SafeModeListPendingOutput(
        total=len(items),
        items=[_item_to_dict(i) for i in items],
    )


# ---------------------------------------------------------------------------
# bsvibe_safe_mode_approve — flips pending → approved AND dispatches.
# ---------------------------------------------------------------------------
class SafeModeApproveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: uuid.UUID


class SafeModeActionOutput(_Output):
    item_id: str
    status: str
    dispatched: bool


async def _h_approve(args: SafeModeApproveInput, ctx: ToolContext) -> Any:
    queue = SafeModeQueue(ctx.session)
    pending = {
        item.id: item for item in await queue.list_pending(workspace_id=ctx.principal.workspace_id)
    }
    item = pending.get(args.item_id)
    if item is None:
        raise ToolError(f"no pending Safe Mode item {args.item_id}")
    deliverable_id = item.deliverable_id

    ok = await queue.approve(
        workspace_id=ctx.principal.workspace_id,
        item_id=args.item_id,
        actor_id=ctx.principal.user_id,
    )
    if not ok:
        raise ToolError(f"Safe Mode item {args.item_id} is no longer pending")
    await ctx.session.commit()

    # Lift E40 — mirror the REST `POST /api/v1/safemode/{id}/approve` parity
    # ([[bsvibe-mcp-ui-parity]]): the MCP path MUST run the outbound dispatch
    # through the same ``dispatch_delivery`` helper the REST route uses so
    # an approved item lands as a real PR / page / channel post instead of
    # rotting in the approved state. Pre-E40 the handler only flipped the
    # queue row and returned ``dispatched=False``, relying on "the worker's
    # next tick" — but the worker drains ``delivery_events``, not the
    # safe_mode queue. The dogfood retrace (run 1079bff5, 2026-06-17)
    # caught this: the run reached ``review_ready``, the MCP tool flipped
    # the queue row, but the PR never opened. Approval stays irreversible:
    # a transient dispatch failure does NOT revert the approve.
    dispatched = await _dispatch_approved(ctx, deliverable_id=deliverable_id)

    return SafeModeActionOutput(
        item_id=str(args.item_id),
        status="approved",
        dispatched=dispatched,
    )


async def _dispatch_approved(ctx: ToolContext, *, deliverable_id: uuid.UUID) -> bool:
    """The ONE outbound path both MCP approves share (item-scoped + run-scoped).

    Lift E40 established that MCP approve must dispatch through the same
    ``dispatch_delivery`` helper the REST route uses — the worker drains
    ``delivery_events``, not the safe_mode queue, so a flipped row alone never
    ships. This helper additionally persists the **compensation handle**, which
    the MCP path was silently dropping.

    Every other approve surface captures it — REST
    (``api/v1/safemode/mutations.py``), the Telegram callback
    (``connectors/approval_callback.py``), and the delivery worker itself — so a
    deliverable approved via MCP was the only one that could never be
    RETRACTED: the plugin-private revert token was thrown away while the call
    still returned ``dispatched=True``. Nothing looked wrong until someone tried
    to undo it.

    Approval stays irreversible: a transient dispatch failure does NOT revert
    the approve, it just leaves ``dispatched=False``.
    """
    dispatcher = await _resolve_delivery_dispatcher(ctx)
    if dispatcher is None:
        logger.warning(
            "mcp_safe_mode_approve_no_dispatcher_configured",
            deliverable_id=str(deliverable_id),
            hint=(
                "wire a delivery_dispatcher into ToolContext.extras at MCP "
                "server boot to enable end-to-end approve+dispatch parity"
            ),
        )
        return False

    artifact_type = await _artifact_type_for_deliverable(ctx.session, deliverable_id)
    try:
        result = await dispatch_delivery(
            dispatcher,
            workspace_id=ctx.principal.workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,
            # Run auto-resolve seam: on delivery success, resolve this run's
            # pending review Decision + ship the run within this session.
            session=ctx.session,
        )
    except Exception:  # noqa: BLE001 — approval already committed; dispatch is best-effort
        logger.warning(
            "mcp_safe_mode_approve_dispatch_failed",
            deliverable_id=str(deliverable_id),
            exc_info=True,
        )
        return False

    # B12b — without this the retract endpoint has nothing to revert through.
    await persist_compensation_handles(ctx.session, deliverable_id=deliverable_id, result=result)
    return True


# ---------------------------------------------------------------------------
# bsvibe_safe_mode_approve_run — the RUN-scoped twin (MCP<->REST parity).
# REST carried ``approve_run`` from the start (B12a); MCP had only the
# item-scoped approve, so the SAME leak #769 fixed on the deny side survived
# on the approve side: a multi-artifact run leaves one queue row per partial,
# and closing one row leaves the rest pointing at no notification at all.
# ---------------------------------------------------------------------------
class SafeModeApproveRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID


class SafeModeRunApproveOutput(_Output):
    run_id: str
    approved_count: int
    dispatched_count: int


async def _h_approve_run(args: SafeModeApproveRunInput, ctx: ToolContext) -> Any:
    queue = SafeModeQueue(ctx.session)
    pending = await queue.list_pending_for_run(
        workspace_id=ctx.principal.workspace_id, run_id=args.run_id
    )
    if not pending:
        raise ToolError(f"no pending Safe Mode items for run {args.run_id}")

    # Snapshot (item_id, deliverable_id) BEFORE approving so the dispatch loop
    # does not depend on the mutated queue rows.
    targets = [(item.id, item.deliverable_id) for item in pending]
    approved: list[uuid.UUID] = []
    for item_id, _ in targets:
        if await queue.approve(
            workspace_id=ctx.principal.workspace_id,
            item_id=item_id,
            actor_id=ctx.principal.user_id,
        ):
            approved.append(item_id)
    await ctx.session.commit()

    dispatched = 0
    for item_id, deliverable_id in targets:
        if item_id in approved and await _dispatch_approved(ctx, deliverable_id=deliverable_id):
            dispatched += 1

    return SafeModeRunApproveOutput(
        run_id=str(args.run_id),
        approved_count=len(approved),
        dispatched_count=dispatched,
    )


async def _resolve_delivery_dispatcher(ctx: ToolContext) -> Any:
    """Return the outbound :class:`PluginDispatchAdapter` for this call.

    The MCP context's static-import surface is intentionally narrow
    (import-contract `MCP context depends only on Identity + Workflow +
    Knowledge + common`), so the dispatcher factory — which transitively
    pulls connector plugins via ``backend.extensions`` — cannot be
    imported HERE at module level. Instead the MCP server boot
    (:mod:`backend.mcp.streamable_http`) builds the dispatcher once and
    installs it into every :class:`ToolContext` via
    ``ctx.extras["delivery_dispatcher"]``. Tests inject the same key with
    a stub.

    Returns ``None`` when no dispatcher is wired so the caller can fall
    back to ``dispatched=False`` instead of crashing the approve.
    """
    return ctx.extras.get("delivery_dispatcher") if ctx.extras else None


async def _artifact_type_for_deliverable(session: Any, deliverable_id: uuid.UUID) -> str:
    """Resolve the deliverable's artifact_type for ``dispatch_delivery``.
    Mirrors :func:`backend.api.v1.safemode._helpers._artifact_type_for`."""
    from backend.workflow.infrastructure.db import Deliverable  # noqa: PLC0415

    deliverable = await session.get(Deliverable, deliverable_id)
    if deliverable is None:
        return "direct_output"
    return str(deliverable.deliverable_type.value)


# ---------------------------------------------------------------------------
# bsvibe_safe_mode_deny — flips pending → denied. No dispatch.
# ---------------------------------------------------------------------------
class SafeModeDenyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: uuid.UUID
    reason: str = Field("", max_length=2000)


async def _h_deny(args: SafeModeDenyInput, ctx: ToolContext) -> Any:
    queue = SafeModeQueue(ctx.session)
    ok = await queue.deny(
        workspace_id=ctx.principal.workspace_id,
        item_id=args.item_id,
        actor_id=ctx.principal.user_id,
        reason=args.reason,
    )
    if not ok:
        raise ToolError(f"no pending Safe Mode item {args.item_id}")
    await ctx.session.commit()
    return SafeModeActionOutput(
        item_id=str(args.item_id),
        status="denied",
        dispatched=False,
    )


# ---------------------------------------------------------------------------
# bsvibe_safe_mode_deny_run — the RUN-scoped twin (MCP↔REST parity).
# Safe Mode is a per-Run transactional container (B12a): a multi-artifact run
# leaves one queue row per partial. Item-scoped deny alone left every row the
# founder did not individually name pending forever.
# ---------------------------------------------------------------------------
class SafeModeDenyRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID
    reason: str = Field("", max_length=2000)


class SafeModeRunActionOutput(_Output):
    run_id: str
    denied_count: int


async def _h_deny_run(args: SafeModeDenyRunInput, ctx: ToolContext) -> Any:
    queue = SafeModeQueue(ctx.session)
    pending = await queue.list_pending_for_run(
        workspace_id=ctx.principal.workspace_id, run_id=args.run_id
    )
    if not pending:
        raise ToolError(f"no pending Safe Mode items for run {args.run_id}")
    denied = 0
    for item in pending:
        if await queue.deny(
            workspace_id=ctx.principal.workspace_id,
            item_id=item.id,
            actor_id=ctx.principal.user_id,
            reason=args.reason,
        ):
            denied += 1
    await ctx.session.commit()
    return SafeModeRunActionOutput(run_id=str(args.run_id), denied_count=denied)


# ---------------------------------------------------------------------------
# bsvibe_safe_mode_get / bsvibe_safe_mode_set — the workspace mode toggle.
# L3 (#5) — MCP parity for the Settings → General Safe / Auto control. Safe
# (True) holds every deliverable for approval; Auto (False) auto-dispatches
# (the delivery gate is bypassed — real blocks like ask_user_question /
# verification failures still surface as Decisions regardless).
# ---------------------------------------------------------------------------
class SafeModeGetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SafeModeModeOutput(_Output):
    safe_mode: bool


async def _h_get_mode(args: SafeModeGetInput, ctx: ToolContext) -> Any:
    workspace = await ctx.session.get(WorkspaceRow, ctx.principal.workspace_id)
    if workspace is None:
        raise ToolError("workspace not found")
    return SafeModeModeOutput(safe_mode=bool(workspace.safe_mode))


class SafeModeSetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    safe_mode: bool


async def _h_set_mode(args: SafeModeSetInput, ctx: ToolContext) -> Any:
    workspace = await ctx.session.get(WorkspaceRow, ctx.principal.workspace_id)
    if workspace is None:
        raise ToolError("workspace not found")
    workspace.safe_mode = args.safe_mode
    await ctx.session.commit()
    return SafeModeModeOutput(safe_mode=bool(workspace.safe_mode))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_safe_mode_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_safe_mode_list_pending",
            description=(
                "List Safe Mode queue items awaiting founder approval in the "
                "active workspace. Pass `run_id` to narrow to one run's group."
            ),
            input_schema=SafeModeListPendingInput,
            output_schema=SafeModeListPendingOutput,
            handler=_h_list_pending,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_safe_mode_approve",
            description=(
                "Approve one queued Safe Mode item and dispatch its deliverable. "
                "Approval is irreversible; a transient connector failure does not "
                "revert it (matches the PWA behaviour)."
            ),
            input_schema=SafeModeApproveInput,
            output_schema=SafeModeActionOutput,
            handler=_h_approve,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.safe_mode_approve.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_safe_mode_approve_run",
            description=(
                "Approve EVERY pending Safe Mode item for one run and dispatch "
                "each deliverable. Safe Mode is a per-run transaction — "
                "approving item-by-item leaves the rest of the run pending "
                "forever, reachable by no notification."
            ),
            input_schema=SafeModeApproveRunInput,
            output_schema=SafeModeRunApproveOutput,
            handler=_h_approve_run,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.safe_mode_approve_run.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_safe_mode_deny",
            description=(
                "Deny one queued Safe Mode item with an optional reason. No "
                "dispatch — the deliverable is dropped."
            ),
            input_schema=SafeModeDenyInput,
            output_schema=SafeModeActionOutput,
            handler=_h_deny,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.safe_mode_deny.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_safe_mode_deny_run",
            description=(
                "Deny EVERY pending Safe Mode item for one run, with an optional "
                "reason recorded on each. Safe Mode is a per-run transaction — "
                "denying item-by-item leaves the rest of the run pending forever. "
                "Leave the reason EMPTY for housekeeping: a blank reason is not "
                "captured as a teaching signal."
            ),
            input_schema=SafeModeDenyRunInput,
            output_schema=SafeModeRunActionOutput,
            handler=_h_deny_run,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.safe_mode_deny_run.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_safe_mode_get",
            description=(
                "Get the active workspace's Safe Mode setting. `true` (Safe) holds "
                "every deliverable for approval; `false` (Auto) auto-dispatches."
            ),
            input_schema=SafeModeGetInput,
            output_schema=SafeModeModeOutput,
            handler=_h_get_mode,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_safe_mode_set",
            description=(
                "Switch the active workspace between Safe Mode (`safe_mode=true`, "
                "every deliverable is held for approval) and Auto (`safe_mode=false`, "
                "deliverables auto-dispatch). Real blocks (ask_user_question / "
                "verification failures) still surface as Decisions either way."
            ),
            input_schema=SafeModeSetInput,
            output_schema=SafeModeModeOutput,
            handler=_h_set_mode,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.safe_mode_set.invoked",
        )
    )


__all__ = ["register_safe_mode_tools"]
