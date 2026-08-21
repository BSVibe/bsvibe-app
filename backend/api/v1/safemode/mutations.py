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
from backend.workflow.application.safe_mode_approval import (
    approve_and_dispatch,
    approve_run_and_dispatch,
)
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.workers.delivery_worker import (
    PluginDispatchAdapter,
)

from ._helpers import get_delivery_dispatcher
from ._schemas import (
    SafeModeActionResponse,
    SafeModeDenyRequest,
    SafeModeRunApproveResponse,
    SafeModeRunDenyResponse,
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
    approved_count, dispatched = await approve_run_and_dispatch(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        actor_id=user.id,
        dispatcher=dispatcher,
    )
    if approved_count == 0 and dispatched == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending Safe Mode items for run {run_id}",
        )

    return SafeModeRunApproveResponse(
        run_id=run_id,
        approved_count=approved_count,
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
    outcome = await approve_and_dispatch(
        session,
        workspace_id=workspace_id,
        item_id=item_id,
        actor_id=user.id,
        dispatcher=dispatcher,
    )
    if not outcome.found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending Safe Mode item {item_id}",
        )
    if not outcome.approved:  # lost a race — re-fetched as no longer pending
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Safe Mode item {item_id} is no longer pending",
        )
    # ``dispatched`` 는 이제 실제 결과다. 예전에는 ``True`` 를 하드코딩했고, dispatch 가
    # 터지면 **승인이 이미 커밋된 뒤** HTTP 500 이 나갔다 — 재시도하면 404 였다.
    return SafeModeActionResponse(item_id=item_id, status="approved", dispatched=outcome.dispatched)


@router.post("/runs/{run_id}/deny")
async def deny_run(
    run_id: uuid.UUID,
    body: SafeModeDenyRequest,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    user: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SafeModeRunDenyResponse:
    """Deny ALL pending Safe Mode items for one Run — the twin of
    :func:`approve_run`.

    Approve had a per-run route from the start (B12a) and deny did not, so a
    multi-artifact run could only be refused one item at a time. Every item the
    founder did not individually name stayed pending forever.

    The reason is recorded on EVERY item: it is one decision about one run, and
    the capture path (#760) reads ``deny_reason`` per row. An empty reason stays
    empty — a blank string must never be mistaken for a recorded judgement.
    """
    queue = SafeModeQueue(session)
    pending = await queue.list_pending_for_run(workspace_id=workspace_id, run_id=run_id)
    if not pending:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending Safe Mode items for run {run_id}",
        )
    denied = 0
    for item in pending:
        if await queue.deny(
            workspace_id=workspace_id,
            item_id=item.id,
            actor_id=user.id,
            reason=body.reason,
        ):
            denied += 1
    await session.commit()
    return SafeModeRunDenyResponse(run_id=run_id, denied_count=denied)


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
