"""/api/v1/deliverables — read API for Deliverable rows.

Read-only on the HTTP surface; deliverables are *produced* by the agent loop /
workers on a verified run (Bundle G), never directly by an HTTP POST. The PWA
Brief's "recently shipped" reads this to surface real artifacts.

The ``payload`` column is free-form JSON written by the orchestrator and shaped
``{summary, artifact_refs}``; we map it defensively (missing/odd values degrade
to ``None`` / ``[]``) so a malformed row never 500s the response model.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.config import get_settings
from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    VerificationOutcome,
    VerificationResult,
)

router = APIRouter()

# Read cap for artifact content. A produced source file is small; this guards
# against an accidental multi-MB log/blob slipping into a JSON body. Beyond it
# the response carries the first ``_MAX_CONTENT_BYTES`` decoded as text with
# ``truncated: true`` so the viewer can show a calm "showing the first part"
# note rather than streaming an unbounded payload.
_MAX_CONTENT_BYTES = 256 * 1024


class DeliverableResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    deliverable_type: DeliverableType
    summary: str | None = None
    artifact_refs: list[str] = []
    artifact_uri: str | None = None
    diff_url: str | None = None
    created_at: datetime


class VerificationReport(BaseModel):
    """One VerificationResult — the "how BSVibe checked this" proof.

    ``contract`` is the work LLM's declared list of checks (the checks BSVibe
    promised to run) and ``result`` is the execution outcome of running them;
    both are free-form JSON (shape varies by verifier), so they are surfaced
    verbatim and rendered defensively by the report view.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    outcome: VerificationOutcome
    contract: dict[str, Any] = {}
    result: dict[str, Any] = {}
    created_at: datetime


class DeliverableReportResponse(BaseModel):
    """The glass-box proof for one shipped deliverable: the founder's original
    request, the artifact, and the verification(s) recorded for its producing
    run. ``request`` is the founder's Direction that led to this work (pulled
    from the producing run's free-form payload), so the report reads as a
    document — request → what was built → how it was checked. ``None`` when the
    run carries no recorded intent."""

    model_config = ConfigDict(extra="forbid")

    deliverable: DeliverableResponse
    request: str | None = None
    verifications: list[VerificationReport] = []


def _request_text_of(payload: dict[str, Any]) -> str | None:
    """Pull the founder's Direction out of the producing run's free-form payload
    (``intent_text`` from intake, or ``text``), the same keys the run-detail
    trigger context reads. ``None`` when neither is a non-empty string."""
    for key in ("intent_text", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class ArtifactContentResponse(BaseModel):
    """The produced CONTENT of one artifact file, read-only.

    Served from the persisted run workspace
    (``<run_workspace_root>/<run_id>/<ref>``) so the founder can SEE what the
    agent actually wrote — not just a filename or a (often-null) git link.

    ``content`` is the file decoded as UTF-8 text with ``errors="replace"``
    (lossy but never throws), capped at 256 KiB. ``truncated`` flags that the
    file was larger than the cap (only the leading bytes are returned).
    ``binary`` flags a non-text file, in which case ``content`` is a short
    "binary file, N bytes" note rather than the raw bytes.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str
    content: str
    truncated: bool = False
    binary: bool = False


def _summary_of(payload: dict[str, Any]) -> str | None:
    """Pull a string ``summary`` out of the free-form payload, else ``None``."""
    value = payload.get("summary")
    return value if isinstance(value, str) else None


def _artifact_refs_of(payload: dict[str, Any]) -> list[str]:
    """Pull a list of string ``artifact_refs`` out of the payload, else ``[]``."""
    value = payload.get("artifact_refs")
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _to_response(row: Deliverable) -> DeliverableResponse:
    payload = row.payload if isinstance(row.payload, dict) else {}
    return DeliverableResponse(
        id=row.id,
        run_id=row.run_id,
        workspace_id=row.workspace_id,
        deliverable_type=row.deliverable_type,
        summary=_summary_of(payload),
        artifact_refs=_artifact_refs_of(payload),
        artifact_uri=row.artifact_uri,
        diff_url=row.diff_url,
        created_at=row.created_at,
    )


def _to_verification(row: VerificationResult) -> VerificationReport:
    contract = row.contract if isinstance(row.contract, dict) else {}
    result = row.result if isinstance(row.result, dict) else {}
    return VerificationReport(
        id=row.id,
        outcome=row.outcome,
        contract=contract,
        result=result,
        created_at=row.created_at,
    )


@router.get("")
async def list_deliverables(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    run_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[DeliverableResponse]:
    """List recent Deliverable rows for the workspace, newest first.

    Optional ``run_id`` narrows to one run's deliverables.
    """
    limit = max(1, min(limit, 200))
    stmt = select(Deliverable).where(Deliverable.workspace_id == workspace_id)
    if run_id is not None:
        stmt = stmt.where(Deliverable.run_id == run_id)
    stmt = stmt.order_by(Deliverable.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return [_to_response(row) for row in result.scalars().all()]


@router.get("/{deliverable_id}")
async def get_deliverable(
    deliverable_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DeliverableResponse:
    """Fetch one Deliverable by id, scoped to the caller's workspace."""
    row = await session.get(Deliverable, deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )
    return _to_response(row)


@router.get("/{deliverable_id}/report")
async def get_deliverable_report(
    deliverable_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DeliverableReportResponse:
    """The glass-box proof for one deliverable, scoped to the caller's workspace.

    Returns the deliverable (summary, artifact_refs, artifact_uri, diff_url,
    type, created_at) PLUS the ``VerificationResult`` rows recorded for its
    producing ``run_id`` — each carrying the declared ``contract`` (the checks
    BSVibe promised to run), the ``result`` of running them, and the ``outcome``
    verdict. 404 when the deliverable isn't in the caller's workspace. A run
    with no verification yields a calm empty list rather than erroring.
    """
    row = await session.get(Deliverable, deliverable_id)
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
    verifications = [_to_verification(v) for v in result.scalars().all()]

    # The founder's Direction that led to this work — pulled from the producing
    # run's free-form payload so the report reads request → built → checked. A
    # missing run (cleaned history) degrades to no request, never a 500.
    run = await session.get(ExecutionRun, row.run_id)
    request = (
        _request_text_of(run.payload)
        if run is not None and run.workspace_id == workspace_id and isinstance(run.payload, dict)
        else None
    )

    return DeliverableReportResponse(
        deliverable=_to_response(row),
        request=request,
        verifications=verifications,
    )


def _looks_binary(raw: bytes) -> bool:
    """Heuristic binary sniff: a NUL byte in the inspected prefix → binary.

    Mirrors git's own "is this a text file" test (a NUL in the first 8 KiB).
    Cheap, dependency-free, and deliberately conservative — a stray NUL makes
    us report metadata-only rather than dumping mojibake into a JSON string.
    """
    return b"\x00" in raw[:8192]


@router.get("/{deliverable_id}/artifacts/{ref:path}")
async def get_deliverable_artifact(
    deliverable_id: uuid.UUID,
    ref: str,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ArtifactContentResponse:
    """Serve one artifact file's CONTENT, read-only, scoped to the caller.

    The file is read from the deliverable's PERSISTED run workspace
    (``<run_workspace_root>/<run_id>/<ref>``) — the orchestrator/worker drives
    each run inside that dir and the work LLM's writes land there, so no
    orchestrator/git change is needed to surface real content.

    Security (all 404 — never leak existence/contents across the boundary):
      * workspace scope — the deliverable must belong to the caller's workspace;
      * ref whitelist — ``ref`` MUST be one of the deliverable's own
        ``payload.artifact_refs`` (arbitrary paths are refused outright);
      * path traversal — the resolved realpath MUST stay within the run dir, so
        a whitelisted-but-malicious ``../`` ref cannot escape;
      * missing file — a cleaned run dir / absent file 404s calmly.

    Content is UTF-8 with ``errors="replace"``, capped at 256 KiB
    (``truncated: true`` past the cap). A binary file yields a short
    "binary file, N bytes" note (``binary: true``) instead of raw bytes.
    """
    row = await session.get(Deliverable, deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deliverable {deliverable_id} not found",
        )

    # Ref whitelist: only the deliverable's own declared artifact_refs are
    # serveable — never an arbitrary path the caller supplies.
    payload = row.payload if isinstance(row.payload, dict) else {}
    if ref not in _artifact_refs_of(payload):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="artifact not found for this deliverable",
        )

    settings = get_settings()
    run_dir = (Path(settings.run_workspace_root) / str(row.run_id)).resolve()
    target = (run_dir / ref).resolve()

    # Path-traversal defense: the resolved target must stay within the run dir
    # (catches a whitelisted-but-malicious ``../`` ref). ``is_relative_to`` is
    # the realpath containment check (Py 3.9+).
    if not target.is_relative_to(run_dir):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="artifact not found for this deliverable",
        )

    if not target.is_file():
        # Run dir cleaned, or never written — calm not-found, not a 500.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="artifact content is no longer available",
        )

    raw = target.read_bytes()
    if _looks_binary(raw):
        return ArtifactContentResponse(
            ref=ref,
            content=f"Binary file, {len(raw)} bytes — not shown.",
            truncated=False,
            binary=True,
        )

    truncated = len(raw) > _MAX_CONTENT_BYTES
    text = raw[:_MAX_CONTENT_BYTES].decode("utf-8", errors="replace")
    return ArtifactContentResponse(
        ref=ref,
        content=text,
        truncated=truncated,
        binary=False,
    )
