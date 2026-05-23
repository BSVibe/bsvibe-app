"""/api/v1/decisions — canonicalization queue (proposals + decisions log).

The GET handlers read the per-workspace queue surface. The accept/reject
POSTs let a founder RESOLVE a queued proposal: under the default strict
Safe-Mode policy, merge / create-concept proposals are parked at ``pending``
(the trust-ratchet wall) and only a human approval applies the linked typed
action(s) → canonical anchors / merges.

Resolution runs against the workspace **vault** (FS-as-SoT), where the
canonicalization engine actually persists proposals as markdown notes. The
proposal id in the accept/reject URL is therefore the proposal's vault path
(the engine addresses proposals by ``proposal_path``); it is matched against
the same per-workspace vault root every other knowledge component uses
(``<knowledge_vault_root>/<region>/<workspace_id>/``), so a path that doesn't
belong to the caller's workspace is simply not found there → 404.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import CurrentUser, get_db_session, get_workspace_id
from backend.config import get_settings
from backend.knowledge.canonicalization.db import (
    ActionKind,
    CanonicalizationDecision,
    CanonicalizationProposal,
    DecisionKind,
    ProposalKind,
    ProposalStatus,
)
from backend.knowledge.canonicalization.index import (
    InMemoryCanonicalizationIndex,
    _is_canon_proposal_path,
)
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage

router = APIRouter()


async def build_canonicalization_service(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> CanonicalizationService:
    """Construct a vault-scoped :class:`CanonicalizationService` for the caller.

    Hangs off a :class:`FileSystemStorage` rooted at the same per-workspace
    path the rest of the knowledge stack uses
    (``<knowledge_vault_root>/<region>/<workspace_id>/``), so reads and writes
    are structurally constrained to the caller's workspace — the same boundary
    enforced by :class:`backend.knowledge.factory.KnowledgeFactory`. The index
    + resolver are wired so an accepted merge collapses the variant onto its
    canonical anchor. Safe Mode is irrelevant here (the action already sits at
    ``pending_approval``; ``accept_proposal`` force-approves it).

    Overridable in tests via ``app.dependency_overrides`` to point at a
    fixture vault.
    """
    settings = get_settings()
    vault_root = (
        Path(settings.knowledge_vault_root) / settings.knowledge_default_region / str(workspace_id)
    )
    vault_root.mkdir(parents=True, exist_ok=True)
    storage = FileSystemStorage(vault_root)
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    return CanonicalizationService(
        store=NoteStore(storage),
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
    )


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


# ---------------------------------------------------------------------------
# Resolution — accept / reject a queued proposal (founder approval)
# ---------------------------------------------------------------------------


class ApplyResultResponse(BaseModel):
    """One linked action's apply outcome (mirror of ``models.ApplyResult``)."""

    model_config = ConfigDict(extra="forbid")

    action_path: str
    final_status: str
    affected_paths: list[str]
    error: str | None = None


class AcceptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_path: str
    status: str
    results: list[ApplyResultResponse]


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class RejectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_path: str
    status: str
    reason: str | None = None


def _ensure_addressable(proposal_id: str) -> None:
    """404 unless ``proposal_id`` looks like a canon proposal vault path.

    Guards against arbitrary paths (traversal, non-proposal notes) reaching
    the store. The actual existence + workspace-scope check happens when the
    service reads the path out of the caller's vault.
    """
    if not _is_canon_proposal_path(proposal_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="proposal not found",
        )


@router.post("/{proposal_id:path}/accept")
async def accept_proposal(
    proposal_id: str,
    user: CurrentUser,
    service: Annotated[CanonicalizationService, Depends(build_canonicalization_service)],
) -> AcceptResponse:
    """Accept a queued proposal — apply every linked typed action.

    ``proposal_id`` is the proposal's vault path. Applies the proposal's
    action drafts (e.g. the merge that collapses a variant onto its canonical
    anchor) and marks the proposal ``accepted`` when all linked actions end in
    ``applied``. 404 when the path isn't a proposal in the caller's workspace;
    409 when it is already resolved (not ``pending``).
    """
    _ensure_addressable(proposal_id)
    try:
        results = await service.accept_proposal(proposal_id, actor=user.id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    proposal_status = (
        "accepted" if results and all(r.final_status == "applied" for r in results) else "pending"
    )
    return AcceptResponse(
        proposal_path=proposal_id,
        status=proposal_status,
        results=[
            ApplyResultResponse(
                action_path=r.action_path,
                final_status=r.final_status,
                affected_paths=list(r.affected_paths),
                error=r.error,
            )
            for r in results
        ],
    )


@router.post("/{proposal_id:path}/reject")
async def reject_proposal(
    proposal_id: str,
    user: CurrentUser,
    service: Annotated[CanonicalizationService, Depends(build_canonicalization_service)],
    body: RejectRequest | None = None,
) -> RejectResponse:
    """Reject a queued proposal without applying anything.

    Marks the proposal ``rejected`` (audit-trail evidence records the actor +
    reason). Linked action drafts are left untouched. 404 / 409 mirror
    :func:`accept_proposal`.
    """
    _ensure_addressable(proposal_id)
    reason = body.reason if body is not None else None
    try:
        await service.reject_proposal(proposal_id, actor=user.id, reason=reason)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return RejectResponse(proposal_path=proposal_id, status="rejected", reason=reason)
