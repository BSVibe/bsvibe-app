"""/api/v1/safemode — founder approval gate for outbound deliveries.

Workflow §10.5 (Safe Mode) / §11.2 (deliver-side). When a workspace is in
Safe Mode the :class:`backend.workers.delivery_worker.DeliveryWorker` enqueues
each verified deliverable into the :class:`SafeModeQueue` (status ``pending``)
instead of dispatching it out. This surface lets the founder:

* ``GET  /api/v1/safemode/queue``           — list pending items
* ``POST /api/v1/safemode/{item_id}/approve`` — approve + dispatch out
* ``POST /api/v1/safemode/{item_id}/deny``    — deny (no dispatch)

Approval re-uses the *same* :func:`dispatch_delivery` helper the worker calls
for the Safe-Mode-off path, so there is one outbound-dispatch code path.

The ``compensation_tier`` field is surfaced on each queue item per Workflow
§10.5 (so the founder sees the blast radius before approving). It is a
plugin-level capability and is not derivable without the per-workspace plugin
registry (a later chunk) — until then it is reported as ``None``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import (
    _get_session_factory,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.delivery.safe_mode_queue import SafeModeQueue
from backend.delivery.schema import ArtifactType
from backend.execution.db import Deliverable
from backend.identity.db import UserRow
from backend.workers.delivery_worker import PluginDispatchAdapter, dispatch_delivery
from backend.workers.run import build_delivery_adapter

router = APIRouter()


# ---------------------------------------------------------------------------
# Dispatcher dependency — overridable in tests with an in-test sink.
# ---------------------------------------------------------------------------
async def get_delivery_dispatcher() -> PluginDispatchAdapter:
    """The outbound dispatcher used when a queued delivery is approved.

    Builds the SAME :class:`~backend.delivery.connector_dispatch.ConnectorDeliveryAdapter`
    the Direct path uses (``backend.workers.run.build_delivery_adapter``): it
    loads every connector plugin, carries the settings-derived
    :class:`~backend.accounts.crypto.CredentialCipher`, and opens its own
    session per dispatch (it resolves the workspace's ``connector_accounts``
    delivery binding itself). So an approved delivery shapes + delivers the
    connector outbound event exactly as a Safe-Mode-off delivery does — one
    outbound code path, no connector-shaping duplication.

    The adapter carries the process-wide session factory rather than the
    request-scoped session because it must open a session per dispatch (load the
    Deliverable + resolve the binding). Tests override this dependency to inject
    a connector adapter built against the test session factory, so both code
    paths converge on one adapter.
    """
    return await build_delivery_adapter(session_factory=_get_session_factory())


class SafeModeItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID
    deliverable_id: uuid.UUID
    status: str
    compensation_tier: str | None = None
    expires_at: datetime
    extension_count: int
    created_at: datetime


class SafeModeResolvedResponse(BaseModel):
    """One decided Safe-Mode delivery (the Decisions "Resolved" tab, delivery
    side). ``status`` is the terminal outcome (approved / denied / expired);
    ``decided_at`` is when the founder (or expiry) settled it."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    deliverable_id: uuid.UUID
    status: str
    decided_at: datetime | None = None
    created_at: datetime


class SafeModeDenyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=2000)


class SafeModeActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    status: str
    dispatched: bool


@router.get("/queue")
async def list_queue(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[SafeModeItemResponse]:
    """List pending Safe Mode items awaiting founder approval (newest first)."""
    queue = SafeModeQueue(session)
    items = await queue.list_pending(workspace_id=workspace_id)
    return [
        SafeModeItemResponse(
            id=item.id,
            workspace_id=item.workspace_id,
            deliverable_id=item.deliverable_id,
            status=item.status.value,
            compensation_tier=None,
            expires_at=item.expires_at,
            extension_count=item.extension_count,
            created_at=item.created_at,
        )
        for item in items
    ]


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
    await dispatch_delivery(
        dispatcher,
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type=artifact_type,
    )
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


async def _artifact_type_for(session: AsyncSession, deliverable_id: uuid.UUID) -> ArtifactType:
    """Resolve the deliverable's artifact_type for the dispatch call.

    ``DeliverableType`` values mirror the ``ArtifactType`` literals 1:1; we
    fall back to ``direct_output`` if the deliverable row is gone (the queue
    item still carries the id, but the run could have been purged).
    """
    deliverable = await session.get(Deliverable, deliverable_id)
    if deliverable is None:
        return "direct_output"
    value: str = deliverable.deliverable_type.value
    return value  # type: ignore[return-value]


__all__ = [
    "SafeModeActionResponse",
    "SafeModeDenyRequest",
    "SafeModeItemResponse",
    "get_delivery_dispatcher",
    "router",
]
