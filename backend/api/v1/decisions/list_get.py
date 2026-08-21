"""Read endpoints for ``/api/v1/decisions`` — proposal queue + decisions log.

Strictly read-only adapters over the vault-derived canonicalization index
(FS-as-SoT). Both endpoints address the same per-workspace vault root that
the accept/reject endpoints in :mod:`.resolve` resolve against, so a listed
proposal id round-trips straight back into the resolution path.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex

from ._helpers import _action_handle, build_canonicalization_index
from ._schemas import ProposalResponse

router = APIRouter()


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
