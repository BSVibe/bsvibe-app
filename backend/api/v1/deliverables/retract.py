"""B12b retract — the single mutating deliverables endpoint (Lift §17.9 sub-file).

``POST /api/v1/deliverables/{deliverable_id}/retract`` rolls a delivered
direct-mode artifact back by calling the originating plugin's
``@p.compensate`` handler with the ``compensation_handle`` captured at
delivery time (Workflow §1.2 + §3.1 + §9). The endpoint is the only path
that flips ``retracted_at``.

This module is the thin adapter — parse → :class:`RetractHandler` dispatch
per stored handle → serialize. The plugin-side runtime (handler protocol,
production implementation, plugin-registry-loading factory) lives in
:mod:`._retract_handler`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.v1._workflow_deps import get_deliverable_repository
from backend.workflow.application.deliverable_retraction import RetractHandler
from backend.workflow.application.deliverable_retraction import (
    retract_deliverable as retract_rule,
)
from backend.workflow.domain.repositories import DeliverableRepository

from ._retract_handler import get_retract_handler

logger = structlog.get_logger(__name__)

router = APIRouter()


class RetractedCompensationEntry(BaseModel):
    """One per-stored-handle dispatch outcome (Workflow §3.1)."""

    model_config = ConfigDict(extra="forbid")

    plugin: str
    artifact_type: str
    output: dict[str, Any] = {}


class RetractResponse(BaseModel):
    """The retract endpoint's response shape (Workflow §1.2)."""

    model_config = ConfigDict(extra="forbid")

    deliverable_id: uuid.UUID
    retracted: bool
    retracted_at: datetime
    # B12b — True iff the row was ALREADY retracted before this call (200
    # no-op, the API short-circuited and the per-handle compensate dispatches
    # did NOT re-run). False on the first successful retract. Lets the founder
    # UI render "already retracted" cleanly vs. "just retracted".
    already_retracted: bool = False
    compensated: list[RetractedCompensationEntry] = []


@router.post("/{deliverable_id}/retract")
async def retract_deliverable(
    deliverable_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    handler: Annotated[RetractHandler, Depends(get_retract_handler)],
    deliverables: Annotated[DeliverableRepository, Depends(get_deliverable_repository)],
) -> RetractResponse:
    """Roll a delivered direct-mode artifact back (B12b / Workflow §1.2 + §9).

    A D35 thin adapter: the rule lives in
    :func:`backend.workflow.application.deliverable_retraction.retract_deliverable`
    so the ``bsvibe_deliverables_retract`` MCP tool runs the same one. This
    function only spells that outcome in HTTP:

    * ``404 not_found`` — unknown id, or another workspace's row (existence is
      never leaked across the boundary).
    * ``400 no_compensation_handle`` — nothing captured to revert.
    * ``502 compensate_failed`` — a dispatch raised; the row is NOT marked
      retracted, so the operator can retry.
    * ``200 already_retracted`` — re-retracting is a short-circuit no-op.
    """
    outcome = await retract_rule(
        session,
        deliverables,
        deliverable_id=deliverable_id,
        workspace_id=workspace_id,
        handler=handler,
    )
    if not outcome.found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )
    if outcome.no_handles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no_compensation_handle: deliverable has no captured compensation handles",
        )
    if outcome.failure is not None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"compensate_failed: {outcome.failure}",
        )
    assert outcome.retracted_at is not None  # noqa: S101 — narrowed by the branches above
    return RetractResponse(
        deliverable_id=deliverable_id,
        retracted=True,
        retracted_at=outcome.retracted_at,
        already_retracted=outcome.already_retracted,
        compensated=[
            RetractedCompensationEntry(
                plugin=e.plugin, artifact_type=e.artifact_type, output=e.output
            )
            for e in outcome.compensated
        ],
    )


__all__ = ["router"]
