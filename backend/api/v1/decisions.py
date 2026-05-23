"""/api/v1/decisions — canonicalization queue (proposals + decisions log).

The GET handlers list the per-workspace review queue. Both the list and the
accept/reject POSTs read/write the workspace **vault** (FS-as-SoT) through the
SAME vault-scoped :class:`CanonicalizationService` — proposals are markdown
notes in the vault, NOT rows in the (currently producer-less)
``canonicalization_proposals`` DB table. Listing the DB table returned an
empty queue while real proposals piled up in the vault; sourcing both list and
resolve from the vault makes the queue address ONE store, so a listed proposal
id round-trips straight back into accept/reject.

Under the default strict Safe-Mode policy, merge / create-concept proposals are
parked at ``pending`` (the trust-ratchet wall) and only a human approval
applies the linked typed action(s) → canonical anchors / merges.

The proposal id is the proposal's vault path (the engine addresses proposals by
``proposal_path``); it is matched against the same per-workspace vault root
every other knowledge component uses
(``<knowledge_vault_root>/<region>/<workspace_id>/``), so a path that doesn't
belong to the caller's workspace is simply not found there → 404. Workspace
isolation is therefore structural: each request's service is rooted at the
caller's vault, so another workspace's proposals are never enumerated.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from backend.api.deps import CurrentUser, get_workspace_id
from backend.config import get_settings
from backend.knowledge.canonicalization import models
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


def _vault_root(workspace_id: uuid.UUID) -> Path:
    """``<knowledge_vault_root>/<region>/<workspace_id>/`` for the caller.

    Single source of the per-workspace vault path so the list dependency and
    the resolution service address the exact same store.
    """
    settings = get_settings()
    return (
        Path(settings.knowledge_vault_root) / settings.knowledge_default_region / str(workspace_id)
    )


async def build_canonicalization_index(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> InMemoryCanonicalizationIndex:
    """Read-only vault index for the caller's workspace queue listing.

    Same per-workspace vault root (and therefore the same proposal/decision
    notes) that :func:`build_canonicalization_service` resolves against, so a
    listed proposal id is exactly the path accept/reject will find. The index
    rebuilds from vault markdown alone (Handoff §10), so this is a pure read of
    the FS-as-SoT queue — no DB table, no producer-less store.

    Overridable in tests via ``app.dependency_overrides`` to point at a
    fixture vault.
    """
    vault_root = _vault_root(workspace_id)
    vault_root.mkdir(parents=True, exist_ok=True)
    index = InMemoryCanonicalizationIndex()
    await index.initialize(FileSystemStorage(vault_root))
    return index


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
    vault_root = _vault_root(workspace_id)
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
    """One queued proposal, sourced from the workspace vault.

    ``id`` is the proposal's vault path — the natural handle the resolution
    endpoints address (``POST /api/v1/decisions/{proposal_id:path}/accept``),
    so a listed proposal round-trips straight into accept/reject.
    ``action_kind`` / ``action_path`` are derived from the proposal's first
    linked action draft (``actions/<kind>/...``): the human-readable handle for
    what approving the proposal would mutate. Field set mirrors the previous
    (DB-sourced) response so existing consumers + the PWA contract are
    unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    proposal_kind: str
    action_kind: str
    action_path: str
    status: str
    score: float | None = None
    created_at: datetime
    expires_at: datetime | None = None


class DecisionResponse(BaseModel):
    """One resolved decision-memory note (founder-approval audit trail).

    Sourced from the vault ``decisions/<kind>/...`` notes. ``id`` is the
    decision's vault path; ``decision_kind`` is the directional decision
    (``cannot-link`` / ``must-link``).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    proposal_id: str | None = None
    decision_kind: str
    actor_id: str | None = None
    created_at: datetime


def _action_handle(proposal: models.ProposalEntry) -> tuple[str, str]:
    """Derive ``(action_kind, action_path)`` from a proposal's first draft.

    The proposal links one or more action drafts at ``actions/<kind>/...``;
    the first is the human-readable handle for what approving it touches. Falls
    back to the proposal kind + path when no draft is linked (defensive — every
    proposer emits at least one draft).
    """
    for draft in proposal.action_drafts:
        parts = PurePosixPath(draft).parts
        if len(parts) >= 2 and parts[0] == "actions":
            return parts[1], draft
    return proposal.kind, proposal.path


@router.get("")
async def list_proposals(
    index: Annotated[InMemoryCanonicalizationIndex, Depends(build_canonicalization_index)],
    status_filter: str | None = None,
    limit: int = 50,
) -> list[ProposalResponse]:
    """List canon proposals from the workspace vault, newest first.

    ``status_filter`` defaults to None (all statuses). Pass ``pending`` to
    fetch just the founder-approval queue. Reads vault markdown via the
    canonicalization index (FS-as-SoT) — the same store the accept/reject
    endpoints resolve against — so a returned ``id`` (the proposal's vault
    path) round-trips back into ``POST /{id}/accept``.
    """
    limit = max(1, min(limit, 200))
    proposals = await index.list_proposals(status=status_filter)
    proposals.sort(key=lambda p: p.created_at, reverse=True)
    out: list[ProposalResponse] = []
    for prop in proposals[:limit]:
        action_kind, action_path = _action_handle(prop)
        out.append(
            ProposalResponse(
                id=prop.path,
                proposal_kind=prop.kind,
                action_kind=action_kind,
                action_path=action_path,
                status=prop.status,
                score=prop.proposal_score,
                created_at=prop.created_at,
                expires_at=prop.expires_at,
            )
        )
    return out


@router.get("/log")
async def list_decisions_log(
    index: Annotated[InMemoryCanonicalizationIndex, Depends(build_canonicalization_index)],
    limit: int = 50,
) -> list[DecisionResponse]:
    """List resolved decisions from the vault (the founder-approval audit trail).

    Sources ``decisions/<kind>/...`` notes via the canonicalization index, the
    same FS-as-SoT store the queue listing + accept/reject use.
    """
    limit = max(1, min(limit, 200))
    decisions = await index.list_decisions()
    decisions.sort(key=lambda d: d.created_at, reverse=True)
    return [
        DecisionResponse(
            id=d.path,
            proposal_id=d.source_proposal,
            decision_kind=d.kind,
            actor_id=None,
            created_at=d.created_at,
        )
        for d in decisions[:limit]
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
