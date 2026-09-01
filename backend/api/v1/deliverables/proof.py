"""Verified-proof surface for one shipped deliverable (Lift §17.9 sub-file).

Two endpoints — both glass-box proof reads, both thin adapters (D35):

* ``GET /api/v1/deliverables/{deliverable_id}/report`` — the founder's
  original request, the deliverable, and the recorded VerificationResults
  for its producing run (the "how BSVibe checked this" document).
* ``GET /api/v1/deliverables/{deliverable_id}/artifacts/{ref:path}`` —
  serves one artifact file's CONTENT, read-only, from the persisted run
  workspace via the per-run :class:`ArtifactStore`. Falls back to the
  product main checkout for product-bound runs whose worktree was removed
  after W2 auto-ship.

The captured-diff read (``GET /{id}/diff``) lives in the sibling
:mod:`.diff` sub-file (kept separate to hold each adapter under the D35 ceiling).
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_artifact_store, get_db_session, get_workspace_id
from backend.api.v1._workflow_deps import get_deliverable_repository
from backend.storage.artifact_store import ArtifactStore
from backend.workflow.application.deliverable_artifact import read_deliverable_artifact
from backend.workflow.application.deliverable_report import build_deliverable_report
from backend.workflow.domain.repositories import DeliverableRepository
from backend.workflow.serialization.deliverable_views import (
    ArtifactContentResponse,
    DeliverableReportResponse,
)

from ._narrative_generator import llm_narrative_generator

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/{deliverable_id}/report")
async def get_deliverable_report(
    deliverable_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    deliverables: Annotated[DeliverableRepository, Depends(get_deliverable_repository)],
) -> DeliverableReportResponse:
    """The glass-box proof for one deliverable, scoped to the caller's workspace.

    A D35 thin adapter: the composition lives in
    :func:`backend.workflow.application.deliverable_report.build_deliverable_report`
    so ``bsvibe_deliverables_report`` runs the same one. 404 when the
    deliverable isn't in the caller's workspace.
    """
    report = await build_deliverable_report(
        deliverable_id=deliverable_id,
        workspace_id=workspace_id,
        session=session,
        deliverables=deliverables,
        narrative_generator=llm_narrative_generator(),
    )
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )
    return report


@router.get("/{deliverable_id}/artifacts/{ref:path}")
async def get_deliverable_artifact(
    deliverable_id: uuid.UUID,
    ref: str,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    deliverables: Annotated[DeliverableRepository, Depends(get_deliverable_repository)],
) -> ArtifactContentResponse:
    """Serve one artifact file's CONTENT, read-only, scoped to the caller.

    A D35 thin adapter: the rule — ref whitelist, traversal guard, 256 KiB cap,
    binary refusal — lives in
    :func:`backend.workflow.application.deliverable_artifact.read_deliverable_artifact`
    so ``bsvibe_deliverables_artifacts`` cannot be laxer than this page. Every
    refusal is a 404, so existence never leaks across the boundary.
    """
    content = await read_deliverable_artifact(
        deliverable_id=deliverable_id,
        ref=ref,
        workspace_id=workspace_id,
        session=session,
        store=store,
        deliverables=deliverables,
    )
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="artifact not found for this deliverable",
        )
    return content


__all__ = ["router"]
