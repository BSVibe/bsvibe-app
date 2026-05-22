"""/api/v1/decisions — canonicalization queue (proposals + decisions log).

Read API for the founder-approval surface. Writes happen via the
canonicalization service (not yet HTTP-exposed in Phase 1 — Bundle G
wires the approve/deny POSTs once the workflow state machine lands).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.knowledge.canonicalization.db import (
    ActionKind,
    CanonicalizationDecision,
    CanonicalizationProposal,
    DecisionKind,
    ProposalKind,
    ProposalStatus,
)

router = APIRouter()


class ProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    proposal_kind: ProposalKind
    action_kind: ActionKind
    action_path: str
    status: ProposalStatus
    score: int | None = None
    created_at: datetime
    expires_at: datetime | None = None


class DecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    proposal_id: uuid.UUID | None = None
    decision_kind: DecisionKind
    actor_id: uuid.UUID | None = None
    created_at: datetime


@router.get("")
async def list_proposals(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    status_filter: ProposalStatus | None = None,
    limit: int = 50,
) -> list[ProposalResponse]:
    """List canon proposals for the workspace, newest first.

    ``status_filter`` defaults to None (all statuses). Pass ``pending`` to
    fetch just the founder-approval queue.
    """
    limit = max(1, min(limit, 200))
    stmt = select(CanonicalizationProposal).where(
        CanonicalizationProposal.workspace_id == workspace_id
    )
    if status_filter is not None:
        stmt = stmt.where(CanonicalizationProposal.status == status_filter)
    stmt = stmt.order_by(CanonicalizationProposal.created_at.desc()).limit(limit)

    rows = (await session.execute(stmt)).scalars().all()
    return [
        ProposalResponse(
            id=r.id,
            proposal_kind=r.proposal_kind,
            action_kind=r.action_kind,
            action_path=r.action_path,
            status=r.status,
            score=r.score,
            created_at=r.created_at,
            expires_at=r.expires_at,
        )
        for r in rows
    ]


@router.get("/log")
async def list_decisions_log(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = 50,
) -> list[DecisionResponse]:
    """List resolved decisions (the founder-approval audit trail)."""
    limit = max(1, min(limit, 200))
    stmt = (
        select(CanonicalizationDecision)
        .where(CanonicalizationDecision.workspace_id == workspace_id)
        .order_by(CanonicalizationDecision.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        DecisionResponse(
            id=r.id,
            proposal_id=r.proposal_id,
            decision_kind=r.decision_kind,
            actor_id=r.actor_id,
            created_at=r.created_at,
        )
        for r in rows
    ]
