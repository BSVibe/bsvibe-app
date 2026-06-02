"""Trust proof-surface endpoints (Lift M4a).

Sub-router of :mod:`backend.api.v1.inside`. Two GETs:

* ``GET /api/v1/inside/trust/fleet`` — every product in the workspace
  with its trend-arrow glyph. Backs the L0 Fleet glance per design §3.
  Per-product entries are calm by design: glyph + plain-language reason
  only; no raw numbers on the glance.
* ``GET /api/v1/inside/trust/{product_id}`` — single-product detail with
  touch time + deposit rate + arrow + contract strength. Backs the L3
  Inside trust strip per design §4.3.

Both endpoints workspace-scoped (RLS-applied via existing middleware +
the ORM auto-filter). Read-only; the metrics are pure aggregations over
audit_outbox + settle_drains + execution_runs rows (design §6).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.workflow.application.metrics.trust_surface import (
    ContractStrength,
    DepositMetric,
    ProductTrust,
    TouchTimeMetric,
    TrendArrow,
    TrendGlyph,
    TrustSurfaceService,
)

router = APIRouter()


# --- Schemas --------------------------------------------------------------


class TouchTimeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_touch_time_hours: float
    decisions_resolved_count: int
    decisions_pending_count: int
    window_days: int


class DepositResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deposit_count: int
    slope_per_day: float
    window_days: int


class TrendArrowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    glyph: TrendGlyph
    reason: str


class ContractStrengthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_steady: bool
    amber_reason: str | None = None


class FleetTrustEntry(BaseModel):
    """One product lane on the Fleet glance."""

    model_config = ConfigDict(extra="forbid")

    product_id: uuid.UUID
    trend_arrow: TrendArrowResponse


class FleetTrustResponse(BaseModel):
    """Workspace-wide product trust glyphs (L0 Fleet)."""

    model_config = ConfigDict(extra="forbid")

    products: list[FleetTrustEntry]


class ProductTrustResponse(BaseModel):
    """Single-product trust detail (L3 Inside trust strip)."""

    model_config = ConfigDict(extra="forbid")

    product_id: uuid.UUID
    touch_time: TouchTimeResponse
    deposit_rate: DepositResponse
    trend_arrow: TrendArrowResponse
    contract_strength: ContractStrengthResponse


# --- Helpers --------------------------------------------------------------


def _touch_to_resp(m: TouchTimeMetric) -> TouchTimeResponse:
    return TouchTimeResponse(
        total_touch_time_hours=m.total_touch_time_hours,
        decisions_resolved_count=m.decisions_resolved_count,
        decisions_pending_count=m.decisions_pending_count,
        window_days=m.window_days,
    )


def _deposit_to_resp(m: DepositMetric) -> DepositResponse:
    return DepositResponse(
        deposit_count=m.deposit_count,
        slope_per_day=m.slope_per_day,
        window_days=m.window_days,
    )


def _arrow_to_resp(a: TrendArrow) -> TrendArrowResponse:
    return TrendArrowResponse(glyph=a.glyph, reason=a.reason)


def _strength_to_resp(s: ContractStrength) -> ContractStrengthResponse:
    return ContractStrengthResponse(is_steady=s.is_steady, amber_reason=s.amber_reason)


def _product_to_resp(p: ProductTrust) -> ProductTrustResponse:
    return ProductTrustResponse(
        product_id=p.product_id,
        touch_time=_touch_to_resp(p.touch_time),
        deposit_rate=_deposit_to_resp(p.deposit_rate),
        trend_arrow=_arrow_to_resp(p.trend_arrow),
        contract_strength=_strength_to_resp(p.contract_strength),
    )


# --- Dependencies ---------------------------------------------------------


async def build_trust_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TrustSurfaceService:
    """Per-request :class:`TrustSurfaceService`.

    Overridable in tests via ``app.dependency_overrides``.
    """
    return TrustSurfaceService(session=session)


# --- Endpoints ------------------------------------------------------------


@router.get("/trust/fleet")
async def fleet_trust(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    service: Annotated[TrustSurfaceService, Depends(build_trust_service)],
) -> FleetTrustResponse:
    """Trust glyphs for every product in the workspace.

    Empty workspace returns ``{products: []}`` — never an error. Per
    design §3.4 there's no SSE / live update; Brief loads this once on
    page load.
    """
    product_ids = await service.list_product_ids(workspace_id)
    entries: list[FleetTrustEntry] = []
    for pid in product_ids:
        arrow = await service.compute_trend_arrow(workspace_id, pid)
        entries.append(FleetTrustEntry(product_id=pid, trend_arrow=_arrow_to_resp(arrow)))
    return FleetTrustResponse(products=entries)


@router.get("/trust/{product_id}")
async def product_trust(
    product_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    service: Annotated[TrustSurfaceService, Depends(build_trust_service)],
) -> ProductTrustResponse:
    """Per-product trust detail for the L3 Inside trust strip.

    Returns the four sub-metrics composed in one round trip. A product
    with no events returns the dormant glyph (``·``) + zero counts —
    same shape, never a 404.
    """
    pt = await service.compute_product_trust(workspace_id, product_id)
    return _product_to_resp(pt)


__all__ = ["router"]
