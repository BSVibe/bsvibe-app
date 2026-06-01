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
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.v1._workflow_deps import get_deliverable_repository
from backend.workflow.domain.repositories import DeliverableRepository

from ._retract_handler import RetractHandler, get_retract_handler

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

    Reads the Deliverable's ``compensation_handles`` (populated at delivery
    time from each successful outbound action's ``compensation_handle``) and
    calls the originating plugin's ``@p.compensate`` handler with each.

    Error semantics (operator-visible; the row is mutated ONLY on success):

    * ``404 not_found`` — unknown id, or the deliverable belongs to another
      workspace (existence is never leaked across the boundary).
    * ``400 no_compensation_handle`` — the row carries no handles (pre-B12b or
      every outbound opted out of compensation); nothing to revert.
    * ``502 compensate_failed`` — at least one compensate dispatch raised; the
      row is NOT marked retracted, so the operator can retry. Idempotent
      plugin handlers re-tolerate the second call.
    * ``200 already_retracted`` — re-retracting an already-retracted row is a
      short-circuit no-op (the plugin handlers are idempotent, but the API
      avoids even attempting them).
    """
    row = await deliverables.get(deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )

    # Idempotency: already retracted → 200 no-op (don't fire compensate twice).
    if row.retracted_at is not None:
        return RetractResponse(
            deliverable_id=deliverable_id,
            retracted=True,
            retracted_at=row.retracted_at,
            already_retracted=True,
            compensated=[],
        )

    handles = list(row.compensation_handles or [])
    if not handles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no_compensation_handle: deliverable has no captured compensation handles",
        )

    compensated: list[RetractedCompensationEntry] = []
    for entry in handles:
        plugin = str(entry.get("plugin") or "")
        artifact_type = str(entry.get("artifact_type") or "")
        handle = entry.get("handle")
        if not plugin or not isinstance(handle, dict):
            # Malformed stored entry — surface as a 502 so the operator sees it
            # rather than silently skipping a delivered artifact.
            logger.warning(
                "retract_malformed_entry",
                deliverable_id=str(deliverable_id),
                entry=entry,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"compensate_failed: malformed compensation entry {entry!r}",
            )
        try:
            output = await handler.compensate(
                plugin=plugin,
                artifact_type=artifact_type,
                handle=handle,
                workspace_id=workspace_id,
            )
        except Exception as exc:  # noqa: BLE001 — surface as 502 + log; do NOT mark retracted
            logger.warning(
                "retract_compensate_failed",
                deliverable_id=str(deliverable_id),
                plugin=plugin,
                artifact_type=artifact_type,
                error=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"compensate_failed: {exc}",
            ) from exc
        compensated.append(
            RetractedCompensationEntry(
                plugin=plugin,
                artifact_type=artifact_type,
                output=output if isinstance(output, dict) else {"result": output},
            )
        )

    # All handlers succeeded — flip retracted_at.
    now = datetime.now(tz=UTC)
    row.retracted_at = now
    await session.commit()
    logger.info(
        "deliverable_retracted",
        deliverable_id=str(deliverable_id),
        workspace_id=str(workspace_id),
        compensated=len(compensated),
    )
    return RetractResponse(
        deliverable_id=deliverable_id,
        retracted=True,
        retracted_at=now,
        already_retracted=False,
        compensated=compensated,
    )
