"""Write endpoints for ``/api/v1/safemode`` — approve / deny queued deliveries.

All approvals dispatch through the SAME :func:`dispatch_delivery` helper the
worker uses for the Safe-Mode-off path, so there is one outbound code path.
Approval is irreversible: a transient connector failure during dispatch does
NOT revert the queue item back to pending (the audit log surfaces the error).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import (
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.identity.db import UserRow
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.workers.delivery_worker import (
    PluginDispatchAdapter,
    dispatch_delivery,
    persist_compensation_handles,
)

from ._helpers import _artifact_type_for, get_delivery_dispatcher
from ._schemas import (
    SafeModeActionResponse,
    SafeModeDenyRequest,
    SafeModeRunApproveResponse,
)

router = APIRouter()


@router.post("/runs/{run_id}/approve")
async def approve_run(
    run_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    user: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    dispatcher: Annotated[PluginDispatchAdapter, Depends(get_delivery_dispatcher)],
) -> SafeModeRunApproveResponse:
    """Approve ALL pending Safe Mode items for one Run (B12a / Workflow §1.2).

    Safe Mode is the per-Run transactional container — a single multi-artifact
    run accumulates N partial Deliver events as N pending queue items. This
    endpoint flips all of them pending→approved AND dispatches each through
    the same :func:`dispatch_delivery` helper the per-item approve uses, so
    there is still ONE outbound code path. Returns 404 when the run has no
    pending items (unknown or already settled). A transient dispatch failure
    on one item does NOT revert the approval — the item stays approved.
    """
    queue = SafeModeQueue(session)
    pending = await queue.list_pending_for_run(workspace_id=workspace_id, run_id=run_id)
    if not pending:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending Safe Mode items for run {run_id}",
        )

    # Capture deliverable_ids BEFORE the approve flips state (the row stays
    # but ``decided_at`` is set; deliverable_id is unaffected, however we
    # snapshot to keep the dispatch loop independent of the queue rows).
    targets: list[tuple[uuid.UUID, uuid.UUID]] = [
        (item.id, item.deliverable_id) for item in pending
    ]
    approved_ids: list[uuid.UUID] = []
    for item_id, _ in targets:
        ok = await queue.approve(workspace_id=workspace_id, item_id=item_id, actor_id=user.id)
        if ok:
            approved_ids.append(item_id)
    await session.commit()

    dispatched = 0
    for item_id, deliverable_id in targets:
        if item_id not in approved_ids:
            continue
        artifact_type = await _artifact_type_for(session, deliverable_id)
        try:
            result = await dispatch_delivery(
                dispatcher,
                workspace_id=workspace_id,
                deliverable_id=deliverable_id,
                artifact_type=artifact_type,
            )
            # B12b — capture compensation_handle onto the Deliverable so the
            # retract endpoint can later revert through @p.compensate. Uses the
            # request-scoped session so the persist sits inside the caller's
            # transaction (no extra factory connection).
            await persist_compensation_handles(
                session, deliverable_id=deliverable_id, result=result
            )
            dispatched += 1
        except Exception as exc:  # noqa: BLE001, S112 — never revert the approval on a dispatch hiccup
            # Approval is irreversible (matches the per-item approve path); a
            # transient connector failure surfaces in the audit log + log here,
            # and on the next worker tick (the worker re-drains shipped events).
            import structlog as _structlog  # noqa: PLC0415 — local; avoid top-level churn

            _structlog.get_logger(__name__).warning(
                "safemode_run_approve_dispatch_failed",
                run_id=str(run_id),
                deliverable_id=str(deliverable_id),
                error=str(exc),
            )
            continue

    return SafeModeRunApproveResponse(
        run_id=run_id,
        approved_count=len(approved_ids),
        dispatched_count=dispatched,
    )


@router.post("/{item_id}/approve")
async def approve_item(
    item_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    user: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    dispatcher: Annotated[PluginDispatchAdapter, Depends(get_delivery_dispatcher)],
) -> SafeModeActionResponse:
    """Flip ``pending → approved`` AND dispatch the deliverable out.

    Dispatch runs through the same :func:`dispatch_delivery` helper the worker
    uses for the Safe-Mode-off path — one outbound code path, no duplication.
    """
    queue = SafeModeQueue(session)
    pending = {item.id: item for item in await queue.list_pending(workspace_id=workspace_id)}
    item = pending.get(item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending Safe Mode item {item_id}",
        )
    deliverable_id = item.deliverable_id

    ok = await queue.approve(workspace_id=workspace_id, item_id=item_id, actor_id=user.id)
    if not ok:  # lost a race — re-fetched as no longer pending
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Safe Mode item {item_id} is no longer pending",
        )
    await session.commit()

    artifact_type = await _artifact_type_for(session, deliverable_id)
    result = await dispatch_delivery(
        dispatcher,
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type=artifact_type,
    )
    # B12b — capture compensation_handle onto the Deliverable so the retract
    # endpoint can later revert through @p.compensate. Uses the request-scoped
    # session so the persist sits inside the caller's transaction.
    await persist_compensation_handles(session, deliverable_id=deliverable_id, result=result)
    return SafeModeActionResponse(item_id=item_id, status="approved", dispatched=True)


@router.post("/{item_id}/deny")
async def deny_item(
    item_id: uuid.UUID,
    body: SafeModeDenyRequest,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    user: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SafeModeActionResponse:
    """Flip ``pending → denied`` — no dispatch."""
    queue = SafeModeQueue(session)
    ok = await queue.deny(
        workspace_id=workspace_id, item_id=item_id, actor_id=user.id, reason=body.reason
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending Safe Mode item {item_id}",
        )
    await session.commit()
    return SafeModeActionResponse(item_id=item_id, status="denied", dispatched=False)


__all__ = ["router"]
