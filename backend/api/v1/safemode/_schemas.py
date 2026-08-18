"""Shared Pydantic schemas for ``/api/v1/safemode`` (Lift M1).

Used by the list (read) + mutations (approve/deny) sub-modules — extracted
here so each endpoint module stays a thin adapter (D35).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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


class SafeModeRunDenyResponse(BaseModel):
    """``POST /api/v1/safemode/runs/{run_id}/deny`` result — the deny twin of
    :class:`SafeModeRunApproveResponse`.

    Safe Mode is a per-RUN transaction, so deny must be reachable at the same
    granularity as approve. Without this route the founder could approve a whole
    run in one call but had to deny it item-by-item — and every item they did not
    name stayed pending forever."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    denied_count: int


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


__all__ = [
    "SafeModeActionResponse",
    "SafeModeDenyRequest",
    "SafeModeItemResponse",
    "SafeModeResolvedResponse",
    "SafeModeRunApproveResponse",
    "SafeModeRunGroupResponse",
]
