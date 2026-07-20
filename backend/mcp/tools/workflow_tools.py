"""Workflow tools — products / runs / deliverables read+write surface.

Renames the legacy BSNexus ``bsnexus_projects_*`` taxonomy to bsvibe-app's
``bsvibe_products_*`` — bsvibe-app's first-class shipping unit is the
Product, not a "Project". Handlers depend on the typed repositories the
REST surface already uses (Workflow §D44/D45) so the MCP wire and the
HTTP wire share one canonical query path.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.identity.workspaces_db import ProductRow
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.workflow.infrastructure.repositories import (
    SqlAlchemyDeliverableRepository,
    SqlAlchemyRunRepository,
)

logger = structlog.get_logger(__name__)


class _Envelope(RootModel[Any]):
    """Permissive output envelope — preserves the natural JSON shape."""


# ---------------------------------------------------------------------------
# Helpers — domain → dict serializers (small, transport-stable shapes).
# ---------------------------------------------------------------------------
def _product_to_dict(row: ProductRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "name": row.name,
        "slug": row.slug,
        "repo_url": row.repo_url,
        "bootstrap_status": row.bootstrap_status,
        "bootstrap_artifacts_count": row.bootstrap_artifacts_count,
        "bootstrap_error": row.bootstrap_error,
        # Lift E9 — per-chunk progress snapshot while ingest is running.
        # ``None`` outside the ingest window or before any chunk has
        # finished; founder UI / MCP caller treats ``None`` as "no
        # incremental signal, fall back to bootstrap_status".
        "bootstrap_progress": row.bootstrap_progress,
        # Free-form product metadata (no lifecycle enum). Always an object,
        # never null. ORM attr is ``product_metadata`` (``metadata`` is
        # reserved by SQLAlchemy); the wire field is ``metadata``.
        "metadata": row.product_metadata,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _run_to_dict(row: Any) -> dict[str, Any]:
    payload = row.payload or {}
    intent: str | None = None
    if isinstance(payload, dict):
        candidate = payload.get("intent_text") or payload.get("text")
        if isinstance(candidate, str):
            intent = candidate
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "product_id": str(row.product_id) if row.product_id else None,
        "request_id": str(row.request_id) if row.request_id else None,
        "status": getattr(row.status, "value", str(row.status)),
        "intent": intent,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _deliverable_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "run_id": str(row.run_id),
        "workspace_id": str(row.workspace_id),
        "deliverable_type": getattr(row.deliverable_type, "value", str(row.deliverable_type)),
        "artifact_uri": row.artifact_uri,
        "diff_url": row.diff_url,
        "retracted_at": row.retracted_at.isoformat() if row.retracted_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ---------------------------------------------------------------------------
# bsvibe_products_list
# ---------------------------------------------------------------------------
class ProductsListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(50, ge=1, le=200)


async def _h_products_list(args: ProductsListInput, ctx: ToolContext) -> Any:
    rows = (
        (
            await ctx.session.execute(
                select(ProductRow)
                .where(ProductRow.workspace_id == ctx.principal.workspace_id)
                .order_by(ProductRow.created_at.desc())
                .limit(args.limit)
            )
        )
        .scalars()
        .all()
    )
    return _Envelope([_product_to_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# bsvibe_products_show
# ---------------------------------------------------------------------------
class ProductsShowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug_or_id: str = Field(..., min_length=1, max_length=64)


async def _resolve_product(ctx: ToolContext, slug_or_id: str) -> ProductRow:
    workspace_id = ctx.principal.workspace_id
    # Try UUID first.
    try:
        pid = uuid.UUID(slug_or_id)
    except ValueError:
        pid = None
    if pid is not None:
        row = await ctx.session.get(ProductRow, pid)
        if row is not None and row.workspace_id == workspace_id:
            return row
    # Fall through to slug.
    row = (
        await ctx.session.execute(
            select(ProductRow).where(
                ProductRow.workspace_id == workspace_id,
                ProductRow.slug == slug_or_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ToolError(f"product not found: {slug_or_id}")
    return row


async def _h_products_show(args: ProductsShowInput, ctx: ToolContext) -> Any:
    row = await _resolve_product(ctx, args.slug_or_id)
    return _Envelope(_product_to_dict(row))


# ---------------------------------------------------------------------------
# bsvibe_products_create — mirrors REST ProductCreate (extra="forbid")
# ---------------------------------------------------------------------------
import re  # noqa: E402

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class ProductsCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=64)
    repo_url: str | None = Field(default=None, max_length=512)

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("slug must match ^[a-z][a-z0-9-]*$")
        return v


async def _h_products_create(args: ProductsCreateInput, ctx: ToolContext) -> Any:
    initial_status = "pending" if args.repo_url else None
    row = ProductRow(
        id=uuid.uuid4(),
        workspace_id=ctx.principal.workspace_id,
        name=args.name,
        slug=args.slug,
        repo_url=args.repo_url,
        bootstrap_status=initial_status,
    )
    ctx.session.add(row)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ToolError(f"slug {args.slug!r} already exists in this workspace") from exc

    # UI parity: mirror the REST create handler's post-commit work so
    # `claude mcp` and PWA Brief → New Product land the same artifact.
    # 1. Init the per-product git workspace (W1). Soft-fail — same as
    #    REST: a transient FS error doesn't undo the row.
    # 2. When repo_url is set, hand off the clone + LLM ingest to the
    #    background scheduler (Lift A v2) — exactly what the REST path
    #    does at this seam.
    from backend.storage.product_workspace import (  # noqa: PLC0415
        ProductWorkspaceError,
        init_product_workspace,
    )

    try:
        await init_product_workspace(row.id)
    except ProductWorkspaceError:
        logger.warning(
            "product_workspace_init_failed_at_mcp_create",
            product_id=str(row.id),
            exc_info=True,
        )

    if args.repo_url and ctx.session_factory is not None:
        from backend.workflow.application.runtime.product_bootstrap_runtime import (  # noqa: PLC0415
            schedule_product_bootstrap,
        )

        try:
            schedule_product_bootstrap(
                product_id=row.id,
                workspace_id=ctx.principal.workspace_id,
                repo_url=args.repo_url,
                session_factory=ctx.session_factory,
            )
        except Exception:  # noqa: BLE001 — soft-fail; row already committed
            logger.warning(
                "product_bootstrap_schedule_failed_at_mcp_create",
                product_id=str(row.id),
                exc_info=True,
            )

    return _Envelope(_product_to_dict(row))


# ---------------------------------------------------------------------------
# bsvibe_products_bootstrap_cancel / bsvibe_products_bootstrap_retry — Lift E13
#
# Recoverability for a wedged bootstrap. The qazasa123 dogfood found a
# product whose ingest had been stuck "ingesting" for 6+ hours (a pre-E8
# container's compile_batch iterating 1377 artifacts at 1800s per stuck
# chunk) — and the founder had no MCP / REST surface to abandon it, only
# slug-churn workarounds (``bsvibe-app-r2``, ``r3``…). These two tools
# close that gap:
#
#   * ``cancel``  — flip an in-flight bootstrap to ``failed`` with a
#                   precise reason; opportunistically ``task.cancel()``
#                   the in-process task if the runtime is hosting one.
#   * ``retry``   — reset the row + re-schedule the same ``repo_url``
#                   under the same product_id (no slug churn).
#
# Both reject the wrong status with a clear ``ToolError`` rather than
# silently no-op'ing (cancel on a terminal status; retry on an in-flight
# status). Both require ``mcp:write`` — the founder is mutating prod
# state, even when the mutation looks innocuous.
# ---------------------------------------------------------------------------
_IN_FLIGHT_STATUSES = frozenset({"pending", "cloning", "analyzing", "ingesting"})
_TERMINAL_STATUSES = frozenset(
    {"complete", "failed", "failed:clone", "failed:ingest", "failed:too_large"}
)


# Lift E20 — subdirectories the optional pre-retry wipe targets. Keeps
# the per-workspace vault structure intact otherwise.
_RESETTABLE_VAULT_SUBDIRS: tuple[str, ...] = (
    "garden",
    "concepts",
    "actions",
    "proposals",
    "code_graph",
)


async def _wipe_workspace_vault_subtrees(ctx: ToolContext) -> None:
    """Best-effort delete the resettable subtrees for ``ctx.principal.workspace_id``.

    The vault root is resolved via the helper that already powers the
    knowledge tools, so any layout change there flows through. Failures
    on a single subdir are logged but never propagated — the retry
    proceeds; the new bootstrap will simply re-create whatever was
    expected to be present.
    """
    import asyncio  # noqa: PLC0415 — only on the wipe path
    import shutil  # noqa: PLC0415 — only on the wipe path
    from pathlib import Path  # noqa: PLC0415 — only on the wipe path

    from backend.mcp.tools._helpers import (  # noqa: PLC0415
        vault_root_for,
        workspace_region,
    )

    region = await workspace_region(ctx.session, ctx.principal.workspace_id)
    vault_root = vault_root_for(region=region, workspace_id=ctx.principal.workspace_id)
    for subdir in _RESETTABLE_VAULT_SUBDIRS:
        target = vault_root / subdir
        if not target.exists():
            continue

        def _do_remove(path: Path = target) -> None:
            shutil.rmtree(path, ignore_errors=True)

        try:
            await asyncio.to_thread(_do_remove)
        except Exception:  # noqa: BLE001 — soft-fail per docstring
            logger.warning(
                "products_bootstrap_retry_vault_wipe_failed",
                workspace_id=str(ctx.principal.workspace_id),
                subdir=subdir,
                exc_info=True,
            )


class ProductsBootstrapCancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug_or_id: str = Field(..., min_length=1, max_length=64)


async def _h_products_bootstrap_cancel(args: ProductsBootstrapCancelInput, ctx: ToolContext) -> Any:
    row = await _resolve_product(ctx, args.slug_or_id)
    status = row.bootstrap_status
    if status not in _IN_FLIGHT_STATUSES:
        raise ToolError(f"no-op — bootstrap is terminal (status={status!r})")

    # Lift E13 — opportunistically cancel the in-process task if the
    # runtime is hosting one. The DB flip is the source of truth (the
    # runtime's silent-fail guard from E8 catches the CancelledError-shaped
    # error and surfaces it cleanly), but a live ``task.cancel()`` stops
    # the wasted compute promptly instead of letting the chunk loop run
    # its current 1800s chunk to completion.
    from backend.workflow.application.runtime.product_bootstrap_runtime import (  # noqa: PLC0415
        get_running_task,
    )

    task = get_running_task(row.id)
    if task is not None and not task.done():
        task.cancel()

    row.bootstrap_status = "failed"
    row.bootstrap_error = "cancelled by founder"
    await ctx.session.commit()
    await ctx.session.refresh(row)
    return _Envelope(_product_to_dict(row))


class ProductsBootstrapRetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug_or_id: str = Field(..., min_length=1, max_length=64)
    # Lift E20 — when the bootstrap pipeline changed (file-dump → code
    # graph) the founder needs a way to start fresh on a previously
    # bootstrapped product without slug-churning. ``vault_reset_on_retry``
    # wipes ``garden/`` / ``concepts/`` / ``actions/`` / ``proposals/`` /
    # ``code_graph/`` for the workspace before re-running. Defense
    # against accidents: the founder must ALSO pass ``confirm_reset=True``
    # in the same call. Default ``False`` keeps current behavior.
    vault_reset_on_retry: bool = Field(
        default=False,
        description=(
            "Wipe the workspace's garden/concepts/actions/proposals/code_graph "
            "before re-running the bootstrap. Destructive — requires "
            "confirm_reset=True in the SAME call."
        ),
    )
    confirm_reset: bool = Field(
        default=False,
        description=(
            "Explicit confirmation flag that MUST also be true when "
            "vault_reset_on_retry=True. Two-key guard against accidental wipes."
        ),
    )


async def _h_products_bootstrap_retry(args: ProductsBootstrapRetryInput, ctx: ToolContext) -> Any:
    row = await _resolve_product(ctx, args.slug_or_id)
    if not row.repo_url:
        raise ToolError("product has no repo_url to bootstrap from")
    status = row.bootstrap_status
    # Allow retry only when the prior bootstrap is terminal — refuse to
    # double-schedule on top of an active in-flight ingest. The cancel
    # tool is the founder's escape hatch for that case.
    if status is not None and status not in _TERMINAL_STATUSES:
        raise ToolError("bootstrap already in flight — call bootstrap_cancel first")

    # Lift E20 — optional pre-retry vault wipe, two-key guarded.
    if args.vault_reset_on_retry:
        if not args.confirm_reset:
            raise ToolError("vault_reset_on_retry requires confirm_reset=True in the same call")
        await _wipe_workspace_vault_subtrees(ctx)

    # Reset the lifecycle fields atomically so the founder UI's next poll
    # sees a clean ``pending`` row rather than a half-stamped one.
    row.bootstrap_status = "pending"
    row.bootstrap_artifacts_count = None
    row.bootstrap_error = None
    row.bootstrap_progress = None
    await ctx.session.commit()
    await ctx.session.refresh(row)

    # Schedule the bootstrap under the same product_id — no slug churn.
    if ctx.session_factory is not None:
        from backend.workflow.application.runtime import (  # noqa: PLC0415
            product_bootstrap_runtime,
        )

        try:
            product_bootstrap_runtime.schedule_product_bootstrap(
                product_id=row.id,
                workspace_id=ctx.principal.workspace_id,
                repo_url=row.repo_url,
                session_factory=ctx.session_factory,
            )
        except Exception:  # noqa: BLE001 — soft-fail; row already reset
            logger.warning(
                "product_bootstrap_retry_schedule_failed",
                product_id=str(row.id),
                exc_info=True,
            )

    return _Envelope(_product_to_dict(row))


# ---------------------------------------------------------------------------
# bsvibe_products_set_metadata — mirror the REST PATCH metadata path
#
# Products have no generic MCP update tool, so this is the write surface for
# the free-form ``metadata`` slot (MCP-UI parity with the REST PATCH). REPLACE
# semantics: the supplied object overwrites the stored dict wholesale (no
# shallow merge), matching the REST contract.
# ---------------------------------------------------------------------------
class ProductsSetMetadataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug_or_id: str = Field(..., min_length=1, max_length=64)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form JSON object stored verbatim on the product. REPLACES the "
            "product's existing metadata wholesale (no shallow merge)."
        ),
    )


async def _h_products_set_metadata(args: ProductsSetMetadataInput, ctx: ToolContext) -> Any:
    row = await _resolve_product(ctx, args.slug_or_id)
    row.product_metadata = args.metadata
    await ctx.session.commit()
    await ctx.session.refresh(row)
    return _Envelope(_product_to_dict(row))


# ---------------------------------------------------------------------------
# bsvibe_runs_list
# ---------------------------------------------------------------------------
class RunsListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_slug_or_id: str | None = Field(default=None, max_length=64)
    limit: int = Field(50, ge=1, le=200)


async def _h_runs_list(args: RunsListInput, ctx: ToolContext) -> Any:
    runs_repo = SqlAlchemyRunRepository(ctx.session)
    rows = await runs_repo.list_by_workspace(ctx.principal.workspace_id, limit=args.limit)
    if args.product_slug_or_id:
        product = await _resolve_product(ctx, args.product_slug_or_id)
        rows = [r for r in rows if r.product_id == product.id]
    return _Envelope([_run_to_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# bsvibe_runs_show
# ---------------------------------------------------------------------------
class RunsShowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID


async def _h_runs_show(args: RunsShowInput, ctx: ToolContext) -> Any:
    runs_repo = SqlAlchemyRunRepository(ctx.session)
    row = await runs_repo.get(args.run_id)
    if row is None or row.workspace_id != ctx.principal.workspace_id:
        raise ToolError(f"run not found: {args.run_id}")
    return _Envelope(_run_to_dict(row))


# ---------------------------------------------------------------------------
# bsvibe_runs_cancel — stop an in-flight (open/running) run
# ---------------------------------------------------------------------------
class RunsCancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=280)


async def _h_runs_cancel(args: RunsCancelInput, ctx: ToolContext) -> Any:
    from backend.workflow.application.run_cleanup import cancel_run  # noqa: PLC0415

    outcome = await cancel_run(
        ctx.session,
        run_id=args.run_id,
        workspace_id=ctx.principal.workspace_id,
        reason=args.reason or "cancelled via MCP",
    )
    if not outcome.found:
        raise ToolError(f"run not found: {args.run_id}")
    if not outcome.cancelled:
        raise ToolError(
            f"run {args.run_id} is {outcome.status}; only an in-flight (open/running) "
            f"run can be cancelled — use bsvibe_runs_discard for a review_ready run"
        )
    await ctx.session.commit()
    return _Envelope({"run_id": str(args.run_id), "status": outcome.status, "cancelled": True})


# ---------------------------------------------------------------------------
# bsvibe_runs_discard — abandon any non-terminal run (incl. review_ready)
# ---------------------------------------------------------------------------
class RunsDiscardInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=280)


async def _h_runs_discard(args: RunsDiscardInput, ctx: ToolContext) -> Any:
    from backend.workflow.application.run_cleanup import discard_run  # noqa: PLC0415

    outcome = await discard_run(
        ctx.session,
        run_id=args.run_id,
        workspace_id=ctx.principal.workspace_id,
        reason=args.reason or "discarded via MCP",
        actor_id=ctx.principal.user_id,
    )
    if outcome is None:
        raise ToolError(f"run not found: {args.run_id}")
    await ctx.session.commit()
    return _Envelope(
        {
            "run_id": str(outcome.run_id),
            "status": outcome.status,
            "cancelled": outcome.cancelled,
            "deliverables_retracted": outcome.deliverables_retracted,
            "deliverables_need_compensation": outcome.deliverables_need_compensation,
            "decisions_resolved": outcome.decisions_resolved,
        }
    )


# ---------------------------------------------------------------------------
# bsvibe_deliverables_list
# ---------------------------------------------------------------------------
class DeliverablesListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID | None = None
    limit: int = Field(50, ge=1, le=200)


async def _h_deliverables_list(args: DeliverablesListInput, ctx: ToolContext) -> Any:
    repo = SqlAlchemyDeliverableRepository(ctx.session)
    rows = await repo.list_by_workspace(
        ctx.principal.workspace_id, run_id=args.run_id, limit=args.limit
    )
    return _Envelope([_deliverable_to_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# bsvibe_deliverables_show
# ---------------------------------------------------------------------------
class DeliverablesShowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deliverable_id: uuid.UUID


async def _h_deliverables_show(args: DeliverablesShowInput, ctx: ToolContext) -> Any:
    repo = SqlAlchemyDeliverableRepository(ctx.session)
    row = await repo.get(args.deliverable_id)
    if row is None or row.workspace_id != ctx.principal.workspace_id:
        raise ToolError(f"deliverable not found: {args.deliverable_id}")
    out = _deliverable_to_dict(row)
    out["payload"] = row.payload if isinstance(row.payload, dict) else {}
    return _Envelope(out)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_workflow_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_products_list",
            description="List products in the active workspace, newest first.",
            input_schema=ProductsListInput,
            output_schema=_Envelope,
            handler=_h_products_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_products_show",
            description="Show one product by slug or UUID, scoped to the active workspace.",
            input_schema=ProductsShowInput,
            output_schema=_Envelope,
            handler=_h_products_show,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_products_create",
            description=(
                "Create a new product in the active workspace. Returns the new row. "
                "Bootstrap from `repo_url` is NOT auto-scheduled here — use the PWA "
                "to start the clone+ingest job."
            ),
            input_schema=ProductsCreateInput,
            output_schema=_Envelope,
            handler=_h_products_create,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.products_create.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_products_bootstrap_cancel",
            description=(
                "Abort an in-flight bootstrap for a product (pending / cloning / "
                "analyzing / ingesting). Flips the row to `failed` with "
                "`bootstrap_error='cancelled by founder'` and opportunistically "
                "cancels the running asyncio task. Use this to recover a wedged "
                "bootstrap without slug-churning a fresh product. No-op on a "
                "terminal status (complete / failed*) — surfaces a ToolError."
            ),
            input_schema=ProductsBootstrapCancelInput,
            output_schema=_Envelope,
            handler=_h_products_bootstrap_cancel,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.products_bootstrap_cancel.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_products_bootstrap_retry",
            description=(
                "Re-trigger bootstrap on an existing product (no slug churn). "
                "Resets bootstrap_status/artifacts_count/error/progress and "
                "schedules the same `repo_url` again. Refuses to retry an "
                "in-flight bootstrap — call bootstrap_cancel first. Requires "
                "the product to carry a repo_url. "
                "⚠️ Optional `vault_reset_on_retry=true` + `confirm_reset=true` "
                "wipes the workspace's garden/concepts/actions/proposals/"
                "code_graph subtrees BEFORE re-running — useful after E20 "
                "to start fresh with the new code-graph ingest pipeline."
            ),
            input_schema=ProductsBootstrapRetryInput,
            output_schema=_Envelope,
            handler=_h_products_bootstrap_retry,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.products_bootstrap_retry.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_products_set_metadata",
            description=(
                "Set the free-form `metadata` JSON object on a product (by slug "
                "or UUID). REPLACES the stored metadata wholesale — send the full "
                "object you want persisted (no shallow merge). The founder's "
                "deliberate alternative to a rigid lifecycle enum: stash the "
                "product's stage / custom attributes / context that agents + "
                "schedules read and write."
            ),
            input_schema=ProductsSetMetadataInput,
            output_schema=_Envelope,
            handler=_h_products_set_metadata,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.products_set_metadata.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_runs_list",
            description=(
                "List recent ExecutionRuns in the active workspace, newest first. "
                "Pass `product_slug_or_id` to narrow to one product."
            ),
            input_schema=RunsListInput,
            output_schema=_Envelope,
            handler=_h_runs_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_runs_show",
            description="Show one ExecutionRun by id, scoped to the active workspace.",
            input_schema=RunsShowInput,
            output_schema=_Envelope,
            handler=_h_runs_show,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_runs_cancel",
            description=(
                "Cancel an in-flight ExecutionRun (status `open` or `running`) → "
                "`cancelled`. Mirrors the PWA/REST cancel action; a cancelled run "
                "is recoverable via retry. Errors on a terminal run, and on a "
                "`review_ready` run (use `bsvibe_runs_discard` for that)."
            ),
            input_schema=RunsCancelInput,
            output_schema=_Envelope,
            handler=_h_runs_cancel,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.runs_cancel.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_runs_discard",
            description=(
                "Discard (폐기) an ExecutionRun — the cleanup primitive for a "
                "`review_ready` run with no Safe Mode entry, or any non-terminal "
                "run. Transitions it to `cancelled`, resolves its pending Decisions "
                "(so its 답변이 필요해요 card leaves the Summary dashboard), "
                "best-effort removes its worktree, and tombstones its handle-less "
                "deliverables (`retracted_at`). Deliverables that carry compensation handles (a "
                "delivered external artifact) are NOT silently tombstoned — they are "
                "returned in `deliverables_need_compensation` for an explicit "
                "compensating retract via POST /deliverables/{id}/retract."
            ),
            input_schema=RunsDiscardInput,
            output_schema=_Envelope,
            handler=_h_runs_discard,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.runs_discard.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_deliverables_list",
            description=(
                "List recent Deliverables in the active workspace, newest first. "
                "Pass `run_id` to narrow to one run's outputs."
            ),
            input_schema=DeliverablesListInput,
            output_schema=_Envelope,
            handler=_h_deliverables_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_deliverables_show",
            description="Show one Deliverable by id (includes its payload).",
            input_schema=DeliverablesShowInput,
            output_schema=_Envelope,
            handler=_h_deliverables_show,
            required_scopes=("mcp:read",),
        )
    )


__all__ = ["register_workflow_tools"]
