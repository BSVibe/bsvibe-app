"""Inside Trust tools — UI-parity Proof-Surface read (Lift D3d).

Wraps the founder-facing trust surface the PWA's Brief
(``TrendArrowGlyph.tsx``) + L3 Inside Trust strip (``TrustPanel.tsx``)
drive via ``/api/v1/inside/trust/*`` (Lift M4a):

* ``GET /api/v1/inside/trust/fleet`` → :func:`bsvibe_inside_trust_fleet`
* ``GET /api/v1/inside/trust/{product_id}`` → :func:`bsvibe_inside_trust_show`

Both surfaces are read-only aggregations over ``execution_runs`` +
``settle_drains`` + ``decisions`` — no second copy of the metric math
lives here, the existing
:class:`backend.workflow.application.metrics.trust_surface.TrustSurfaceService`
is the one canonical chain MCP + REST share. ``mcp:read`` is enough —
nothing here mutates.

Output shape mirrors the REST schemas 1:1 (``FleetTrustResponse`` /
``ProductTrustResponse``) so an LLM holding the OpenAPI for the REST
surface can read the MCP wire without an extra mapping.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.mcp.api import Tool, ToolContext, ToolRegistry
from backend.workflow.application.metrics.trust_surface import (
    ContractStrength,
    DepositMetric,
    ProductTrust,
    TouchTimeMetric,
    TrendArrow,
    TrendGlyph,
    TrustSurfaceService,
)


# ---------------------------------------------------------------------------
# Shared output sub-schemas — 1:1 with backend/api/v1/inside/trust.py
# ---------------------------------------------------------------------------
class _TouchTimeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_touch_time_hours: float
    decisions_resolved_count: int
    decisions_pending_count: int
    window_days: int


class _DepositOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deposit_count: int
    slope_per_day: float
    window_days: int


class _TrendArrowOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    glyph: TrendGlyph
    reason: str


class _ContractStrengthOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_steady: bool
    amber_reason: str | None = None


def _touch_to_out(m: TouchTimeMetric) -> _TouchTimeOut:
    return _TouchTimeOut(
        total_touch_time_hours=m.total_touch_time_hours,
        decisions_resolved_count=m.decisions_resolved_count,
        decisions_pending_count=m.decisions_pending_count,
        window_days=m.window_days,
    )


def _deposit_to_out(m: DepositMetric) -> _DepositOut:
    return _DepositOut(
        deposit_count=m.deposit_count,
        slope_per_day=m.slope_per_day,
        window_days=m.window_days,
    )


def _arrow_to_out(a: TrendArrow) -> _TrendArrowOut:
    return _TrendArrowOut(glyph=a.glyph, reason=a.reason)


def _strength_to_out(s: ContractStrength) -> _ContractStrengthOut:
    return _ContractStrengthOut(is_steady=s.is_steady, amber_reason=s.amber_reason)


# ---------------------------------------------------------------------------
# bsvibe_inside_trust_fleet
# ---------------------------------------------------------------------------
class InsideTrustFleetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FleetEntryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    trend_arrow: _TrendArrowOut


class InsideTrustFleetOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    products: list[FleetEntryOut]


async def _h_fleet(_args: InsideTrustFleetInput, ctx: ToolContext) -> Any:
    service = TrustSurfaceService(session=ctx.session)
    product_ids = await service.list_product_ids(ctx.principal.workspace_id)
    entries: list[FleetEntryOut] = []
    for pid in product_ids:
        arrow = await service.compute_trend_arrow(ctx.principal.workspace_id, pid)
        entries.append(FleetEntryOut(product_id=str(pid), trend_arrow=_arrow_to_out(arrow)))
    return InsideTrustFleetOutput(products=entries)


# ---------------------------------------------------------------------------
# bsvibe_inside_trust_show
# ---------------------------------------------------------------------------
class InsideTrustShowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: uuid.UUID


class InsideTrustShowOutput(BaseModel):
    """Mirror of :class:`ProductTrustResponse` (REST)."""

    model_config = ConfigDict(extra="forbid")

    product_id: str
    touch_time: _TouchTimeOut
    deposit_rate: _DepositOut
    trend_arrow: _TrendArrowOut
    contract_strength: _ContractStrengthOut


def _product_to_out(p: ProductTrust) -> InsideTrustShowOutput:
    return InsideTrustShowOutput(
        product_id=str(p.product_id),
        touch_time=_touch_to_out(p.touch_time),
        deposit_rate=_deposit_to_out(p.deposit_rate),
        trend_arrow=_arrow_to_out(p.trend_arrow),
        contract_strength=_strength_to_out(p.contract_strength),
    )


async def _h_show(args: InsideTrustShowInput, ctx: ToolContext) -> Any:
    service = TrustSurfaceService(session=ctx.session)
    pt = await service.compute_product_trust(ctx.principal.workspace_id, args.product_id)
    return _product_to_out(pt)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_inside_trust_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_inside_trust_fleet",
            description=(
                "Workspace-wide Fleet trust glyphs — one trend-arrow per product. "
                "Mirrors the PWA Brief Fleet glance (`/api/v1/inside/trust/fleet`). "
                "An empty workspace returns an empty product list (never 404)."
            ),
            input_schema=InsideTrustFleetInput,
            output_schema=InsideTrustFleetOutput,
            handler=_h_fleet,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_inside_trust_show",
            description=(
                "Per-product Inside trust strip — touch_time + deposit_rate + "
                "trend_arrow + contract_strength. Mirrors the PWA L3 Inside Trust "
                "panel (`/api/v1/inside/trust/{product_id}`). A product with no "
                "events returns the dormant glyph + zero counts (same shape — no 404)."
            ),
            input_schema=InsideTrustShowInput,
            output_schema=InsideTrustShowOutput,
            handler=_h_show,
            required_scopes=("mcp:read",),
        )
    )


__all__ = ["register_inside_trust_tools"]
