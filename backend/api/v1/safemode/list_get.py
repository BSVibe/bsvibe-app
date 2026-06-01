"""Read endpoints for ``/api/v1/safemode`` — pending queue + resolved log.

Strictly read-only adapters over :class:`SafeModeQueue`. The mutations
(approve / deny) live in :mod:`.mutations`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.workflow.application.safe_mode_queue import SafeModeQueue

from ._helpers import _to_item_response
from ._schemas import (
    SafeModeItemResponse,
    SafeModeResolvedResponse,
    SafeModeRunGroupResponse,
)

router = APIRouter()


@router.get("/queue")
async def list_queue(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[SafeModeItemResponse]:
    """List pending Safe Mode items awaiting founder approval (newest first)."""
    queue = SafeModeQueue(session)
    items = await queue.list_pending(workspace_id=workspace_id)
    return [_to_item_response(item) for item in items]


@router.get("/queue/by-run")
async def list_queue_by_run(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[SafeModeRunGroupResponse]:
    """List pending Safe Mode items grouped by Run (B12a / Workflow §1.2).

    The founder-visible "approve all (N) for this run" surface — Safe Mode is
    the per-Run transactional container for a run's accumulated partial
    Deliver events. Groups are ordered by the most recent item in each group
    (matches the newest-first ordering of :meth:`SafeModeQueue.list_pending`).
    Items with no ``run_id`` (legacy / single-emit) are folded under a single
    ``null`` group so they remain visible until they age out."""
    queue = SafeModeQueue(session)
    items = await queue.list_pending(workspace_id=workspace_id)
    by_run: dict[uuid.UUID | None, list[SafeModeItemResponse]] = {}
    order: list[uuid.UUID | None] = []
    for item in items:
        key: uuid.UUID | None = item.run_id
        if key not in by_run:
            by_run[key] = []
            order.append(key)
        by_run[key].append(_to_item_response(item))
    return [SafeModeRunGroupResponse(run_id=k, items=by_run[k]) for k in order]


@router.get("/resolved")
async def list_resolved(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[SafeModeResolvedResponse]:
    """List decided Safe-Mode deliveries (approved / denied / expired) for the
    Decisions "Resolved" tab, most-recently-decided first."""
    queue = SafeModeQueue(session)
    items = await queue.list_resolved(workspace_id=workspace_id)
    return [
        SafeModeResolvedResponse(
            id=item.id,
            deliverable_id=item.deliverable_id,
            status=item.status.value,
            decided_at=item.decided_at,
            created_at=item.created_at,
        )
        for item in items
    ]


__all__ = ["router"]
