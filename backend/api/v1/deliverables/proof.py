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
from pathlib import Path
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_artifact_store, get_db_session, get_workspace_id
from backend.api.v1._workflow_deps import get_deliverable_repository
from backend.config import get_settings
from backend.storage.artifact_store import ArtifactStore, LocalFilesystemArtifactStore
from backend.workflow.domain.repositories import DeliverableRepository
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    VerificationOutcome,
    VerificationResult,
)

from ._schemas import (
    MAX_CONTENT_BYTES,
    ArtifactContentResponse,
    DeliverableReportResponse,
    artifact_refs_of,
    references_of,
    request_text_of,
    to_response,
    to_verification,
)

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

    Returns the deliverable (summary, artifact_refs, artifact_uri, diff_url,
    type, created_at) PLUS the ``VerificationResult`` rows recorded for its
    producing ``run_id`` — each carrying the declared ``contract`` (the checks
    BSVibe promised to run), the ``result`` of running them, and the ``outcome``
    verdict. 404 when the deliverable isn't in the caller's workspace. A run
    with no verification yields a calm empty list rather than erroring.
    """
    row = await deliverables.get(deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )
    stmt = (
        select(VerificationResult)
        .where(
            VerificationResult.run_id == row.run_id,
            VerificationResult.workspace_id == workspace_id,
        )
        .order_by(VerificationResult.created_at.asc())
    )
    result = await session.execute(stmt)
    verifications = [to_verification(v) for v in result.scalars().all()]
    # B4 trust-integrity: the report is "verified" ONLY when a real PASSED
    # VerificationResult is among the run's recorded verifications — never
    # inferred from the Deliverable existing. A hollow deliverable (none, or only
    # failed/inconclusive) reads as needs-review, honestly.
    verified = any(v.outcome == VerificationOutcome.PASSED for v in verifications)

    # The founder's Direction that led to this work — pulled from the producing
    # run's free-form payload so the report reads request → built → checked. A
    # missing run (cleaned history) degrades to no request, never a 500.
    run = await session.get(ExecutionRun, row.run_id)
    request = (
        request_text_of(run.payload)
        if run is not None and run.workspace_id == workspace_id and isinstance(run.payload, dict)
        else None
    )

    return DeliverableReportResponse(
        deliverable=to_response(row, verified=verified),
        request=request,
        verified=verified,
        verifications=verifications,
        references=references_of(verifications),
    )


def _looks_binary(raw: bytes) -> bool:
    """Heuristic binary sniff: a NUL byte in the inspected prefix → binary.

    Mirrors git's own "is this a text file" test (a NUL in the first 8 KiB).
    Cheap, dependency-free, and deliberately conservative — a stray NUL makes
    us report metadata-only rather than dumping mojibake into a JSON string.
    """
    return b"\x00" in raw[:8192]


async def _read_from_product_main(
    session: AsyncSession, run_id: uuid.UUID, ref: str
) -> bytes | None:
    """Read ``ref`` from the run's product workspace main checkout, or ``None``.

    The W2 ship-time merge lands the run's files under
    ``<product_workspace_root>/<product_id>/`` (the product repo's main checkout)
    and then removes the per-run worktree. A reused
    :class:`LocalFilesystemArtifactStore` rooted at ``product_workspace_root`` and
    keyed by ``product_id`` resolves ``<root>/<product_id>/<ref>`` with the SAME
    centralized traversal guard. ``None`` when the run has no product_id (nothing
    to fall back to) or the file is genuinely absent — the caller maps it to 404.
    """
    run = await session.get(ExecutionRun, run_id)
    if run is None or run.product_id is None:
        return None
    product_store = LocalFilesystemArtifactStore(Path(get_settings().product_workspace_root))
    try:
        return product_store.read_bytes(run.product_id, ref)
    except (ValueError, FileNotFoundError, IsADirectoryError):
        return None


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

    Security (all 404 — never leak existence/contents across the boundary):
      * workspace scope — the deliverable must belong to the caller's workspace;
      * ref whitelist — ``ref`` MUST be one of the deliverable's own
        ``payload.artifact_refs`` (arbitrary paths are refused outright);
      * path traversal — the store's centralized guard refuses any ref that
        resolves outside the run dir (an absolute path / ``../`` segment);
      * missing file — a cleaned run dir / absent file 404s calmly.

    Content is UTF-8 with ``errors="replace"``, capped at 256 KiB
    (``truncated: true`` past the cap). A binary file yields a short
    "binary file, N bytes" note (``binary: true``) instead of raw bytes.
    """
    row = await deliverables.get(deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )

    # Ref whitelist: only the deliverable's own declared artifact_refs are
    # serveable — never an arbitrary path the caller supplies.
    payload = row.payload if isinstance(row.payload, dict) else {}
    if ref not in artifact_refs_of(payload):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="artifact not found for this deliverable",
        )

    try:
        raw = store.read_bytes(row.run_id, ref)
    except ValueError as exc:
        # Traversal / absolute ref — refused by the store's centralized guard.
        # Surface as 404 (never leak existence across the boundary).
        logger.debug("artifact_traversal_refused", run_id=str(row.run_id), ref=ref, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="artifact not found for this deliverable",
        ) from exc
    except FileNotFoundError as exc:
        # W1/W2: a product-bound run's worktree is REMOVED after auto-ship merges
        # it to the product's main, so the produced file no longer lives in the
        # run dir — it lives in the product workspace main checkout. Fall back
        # there before declaring the content gone (else the Files viewer can
        # never open a shipped product run's files). Non-product runs (no main to
        # fall back to) keep the calm 404.
        fallback = await _read_from_product_main(session, row.run_id, ref)
        if fallback is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="artifact content is no longer available",
            ) from exc
        raw = fallback
    except IsADirectoryError as exc:
        # ``ref`` resolves to a directory inside the run dir (e.g. ``src/``).
        # Calm 404 — not a file, no content to serve.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="artifact content is no longer available",
        ) from exc
    if _looks_binary(raw):
        return ArtifactContentResponse(
            ref=ref,
            content=f"Binary file, {len(raw)} bytes — not shown.",
            truncated=False,
            binary=True,
        )

    truncated = len(raw) > MAX_CONTENT_BYTES
    text = raw[:MAX_CONTENT_BYTES].decode("utf-8", errors="replace")
    return ArtifactContentResponse(
        ref=ref,
        content=text,
        truncated=truncated,
        binary=False,
    )
