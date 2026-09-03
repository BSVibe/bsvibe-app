"""Direct tool — submit a founder-direct message into the workflow.

Equivalent to the PWA's Direct button: a one-line submission lands as a
``TriggerEvent(source="direct")`` and the intake/agent/delivery workers
drive it the rest of the way. This tool is the "fix it" trigger an
agentic MCP client uses to dogfood a stuck run from the side.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from backend.config import get_settings
from backend.identity.product_resolution import resolve_product_for_workspace
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.workers.emit import (
    STREAM_INTAKE,
    emit_stream_notification,
    get_emit_redis_client,
)
from backend.workflow.application.intake.direct import DirectTrigger
from backend.workflow.application.run_caps import RunCapReached, enforce_run_cap

logger = structlog.get_logger(__name__)


class DirectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=20000)
    product_slug_or_id: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)


class DirectOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: bool
    duplicate: bool
    workspace_id: str
    product_id: str


async def _resolve_product_id(ctx: ToolContext, slug_or_id: str | None) -> uuid.UUID:
    """L-P1 규칙은 :func:`~backend.identity.product_resolution.
    resolve_product_for_workspace` 가 소유한다 — 여기서는 MCP 의 오류 표면만 얹는다.

    (규칙: 명시 제품(UUID 또는 슬러그) → 가장 먼저 만들어진 제품 → 없음.)
    """
    product_id = await resolve_product_for_workspace(
        ctx.session, workspace_id=ctx.principal.workspace_id, slug_or_id=slug_or_id
    )
    if product_id is None:
        raise ToolError("workspace has no products — create one before submitting a Direct message")
    return product_id


async def _h_direct(args: DirectInput, ctx: ToolContext) -> Any:
    product_id = await _resolve_product_id(ctx, args.product_slug_or_id)

    # 동시 실행 상한 규칙은 :mod:`backend.workflow.application.run_caps` 가
    # 소유한다 — 여기서는 MCP 의 오류 표면만 얹는다. REST 문만 막으면 에이전트
    # 클라이언트가 그대로 우회한다.
    try:
        await enforce_run_cap(ctx.session, workspace_id=ctx.principal.workspace_id)
    except RunCapReached as exc:
        raise ToolError(
            f"workspace already holds {exc.held} concurrent runs (limit {exc.limit}) — "
            "ship or discard a run that is waiting for review, then submit again"
        ) from exc

    trigger = DirectTrigger(ctx.session)
    outcome = await trigger.submit(
        workspace_id=ctx.principal.workspace_id,
        founder_id=ctx.principal.user_id,
        text=args.text,
        product_id=product_id,
        trace_id=args.trace_id,
    )
    await ctx.session.commit()

    # Wake the IntakeWorker stream consumer on the trigger stream. Soft-fail:
    # a Redis hiccup never breaks the accepted submit — DB polling fall-back
    # still picks it up. Skip on duplicates (collapsed submits land no row).
    if not outcome.duplicate:
        try:
            settings = get_settings()
            await emit_stream_notification(
                get_emit_redis_client(settings),
                settings=settings,
                stream=STREAM_INTAKE,
                fields={"workspace_id": str(ctx.principal.workspace_id)},
            )
        except Exception:  # noqa: BLE001 — never break the accepted submit
            logger.warning("mcp_direct_emit_failed", exc_info=True)

    return DirectOutput(
        accepted=True,
        duplicate=outcome.duplicate,
        workspace_id=str(ctx.principal.workspace_id),
        product_id=str(product_id),
    )


def register_direct_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_direct",
            description=(
                "Post a founder-direct message into the workflow — equivalent to "
                "the PWA's Direct button. Idempotent on (founder_id, text); a "
                "double-submit returns `duplicate=true`."
            ),
            input_schema=DirectInput,
            output_schema=DirectOutput,
            handler=_h_direct,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.direct.invoked",
        )
    )


__all__ = ["register_direct_tools"]
