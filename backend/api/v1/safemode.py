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
from backend.execution.db import Deliverable
from backend.identity.db import UserRow
from backend.workers.delivery_worker import (
    PluginDispatchAdapter,
    dispatch_delivery,
    persist_compensation_handles,
)
from backend.workers.run import build_delivery_adapter
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.domain.delivery import ArtifactType

router = APIRouter()


# ---------------------------------------------------------------------------
# Dispatcher dependency — overridable in tests with an in-test sink.
# ---------------------------------------------------------------------------
async def get_delivery_dispatcher() -> PluginDispatchAdapter:
    """The outbound dispatcher used when a queued delivery is approved.

    Builds the SAME :class:`~backend.workflow.application.delivery.connector_dispatch.ConnectorDeliveryAdapter`
    the Direct path uses (``backend.workers.run.build_delivery_adapter``): it
    loads every connector plugin, carries the settings-derived
    :class:`~backend.router.accounts.crypto.CredentialCipher`, and opens its own
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
    # B12a — per-Run grouping key (Workflow §1.2). Nullable for legacy items
    # that pre-date the run_id column.
    run_id: uuid.UUID | None = None
    status: str
    compensation_tier: str | None = None
    expires_at: datetime
    extension_count: int
    created_at: datetime


class SafeModeRunGroupResponse(BaseModel):
    """B12a — pending Safe Mode queue grouped by Run (Workflow §1.2).

    Each group is one Run's accumulated partial Deliver events — the founder
    approves them together via ``POST /api/v1/safemode/runs/{run_id}/approve``.
    Legacy items with no ``run_id`` are surfaced under a single ``null`` group
    so they remain visible until they age out of the queue.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID | None = None
    items: list[SafeModeItemResponse]


class SafeModeRunApproveResponse(BaseModel):
    """B12a — ``POST /api/v1/safemode/runs/{run_id}/approve`` result.

    ``approved_count`` is how many queue items flipped pending→approved;
    ``dispatched_count`` is how many of those were actually dispatched (a
    transient dispatch failure does NOT revert the approval — the item stays
    approved and surfaces on the resolved tab)."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    approved_count: int
    dispatched_count: int


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


def _to_item_response(item: object) -> SafeModeItemResponse:
    """Map a :class:`SafeModeQueueItemRow` to the response shape (B12a — also
    threads ``run_id`` through)."""
    return SafeModeItemResponse(
        id=item.id,  # type: ignore[attr-defined]
        workspace_id=item.workspace_id,  # type: ignore[attr-defined]
        deliverable_id=item.deliverable_id,  # type: ignore[attr-defined]
        run_id=item.run_id,  # type: ignore[attr-defined]
        status=item.status.value,  # type: ignore[attr-defined]
        compensation_tier=None,
        expires_at=item.expires_at,  # type: ignore[attr-defined]
        extension_count=item.extension_count,  # type: ignore[attr-defined]
        created_at=item.created_at,  # type: ignore[attr-defined]
    )


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
    "SafeModeRunApproveResponse",
    "SafeModeRunGroupResponse",
    "get_delivery_dispatcher",
    "router",
]
