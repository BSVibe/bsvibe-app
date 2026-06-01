"""Write endpoints for ``/api/v1/decisions`` — accept / reject a queued proposal.

Both endpoints dispatch through the SAME vault-scoped
:class:`CanonicalizationService` the list reads from, so a listed
``proposal_id`` (the proposal's vault path) is exactly what the resolution
service finds in the caller's vault. A path outside the caller's workspace
simply isn't there → 404.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from backend.api.deps import CurrentUser
from backend.knowledge.canonicalization.service import CanonicalizationService

from ._helpers import _ensure_addressable, build_canonicalization_service
from ._schemas import (
    AcceptResponse,
    ApplyResultResponse,
    RejectRequest,
    RejectResponse,
)

router = APIRouter()


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


__all__ = ["router"]
