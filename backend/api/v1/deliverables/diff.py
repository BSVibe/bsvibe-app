"""Captured-diff proof read for one deliverable (Lift §17.9 sub-file).

``GET /api/v1/deliverables/{deliverable_id}/diff`` — serves the run's captured
old↔new changes as a unified ``git diff`` patch. The diff is captured at
verify-time for product runs (while the run worktree is still alive, before
auto-ship cleanup) and stored on ``Deliverable.payload`` by
:func:`backend.workflow.domain.verified_deliverable.write_verified_deliverable`.

A thin adapter (D35): look the deliverable up scoped to the caller's workspace,
read the stored diff off the payload, serialize. Kept in its own sub-file (not
folded into :mod:`.proof`) so each endpoint-grouping adapter stays under the
D35 250-LOC ceiling.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from backend.api.deps import get_workspace_id
from backend.api.v1._workflow_deps import get_deliverable_repository
from backend.workflow.domain.repositories import DeliverableRepository

from ._schemas import DeliverableDiffResponse, diff_of

router = APIRouter()


@router.get("/{deliverable_id}/diff")
async def get_deliverable_diff(
    deliverable_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    deliverables: Annotated[DeliverableRepository, Depends(get_deliverable_repository)],
) -> DeliverableDiffResponse:
    """Serve the run's captured old↔new changes as a unified ``git diff`` patch.

    Captured at verify-time for product runs (while the run worktree is still
    alive) and stored on ``Deliverable.payload``; the viewer renders it
    GitHub-style red/green. A deliverable with no captured diff — a non-product
    (Direct) run, or a row produced before this feature — returns a calm
    ``diff: null`` (NOT a 404), and the viewer falls back to rendering the
    produced file content as additions. 404 only when the deliverable isn't in
    the caller's workspace (never leak existence across the boundary).
    """
    row = await deliverables.get(deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )
    payload = row.payload if isinstance(row.payload, dict) else {}
    diff, truncated = diff_of(payload)
    return DeliverableDiffResponse(diff=diff, truncated=truncated)
