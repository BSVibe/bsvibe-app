"""Ontology retraction / correction endpoints (Lift M3a).

Sub-router of :mod:`backend.api.v1.inside`. Three POST endpoints:

* ``POST /api/v1/inside/nodes/{node_ref:path}/retract`` — open a retract for
  a garden note. RBAC: editor+. Returns the issued :class:`RetractionSignal`
  + the undo window deadline.
* ``POST /api/v1/inside/nodes/{node_ref:path}/undo-retract`` — undo a
  retract during the 30-second window. Idempotent; returns terminal status.
* ``POST /api/v1/inside/nodes/{node_ref:path}/correct`` — open a correct
  for a garden note (M3a: persistence + audit only; the actual
  field-rewrite lands with M3b alongside the PWA inline editor).

The path matches the design's `/api/v1/inside/nodes/{node_ref}/...`
contract. ``node_ref`` is a vault-relative path for garden notes — the
``:path`` converter is required because the path includes ``/``.

Workspace + actor are resolved from the same auth deps the rest of the
``inside`` surface uses. Writer construction reuses the existing
:class:`KnowledgeFactory` so the vault root is structurally identical to
the read-side endpoints' storage; a retract therefore lands in the same
place the retriever / inspector reads from.

Idempotence: the optional ``correction_id`` lets clients retry safely. A
re-POST with the same id returns the persisted signal + ``created=False``,
NO duplicate audit event.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user_row, get_db_session, get_workspace_id
from backend.api.v1.decisions import _vault_root
from backend.identity.db import UserRow
from backend.knowledge.application.retraction_service import (
    IssueOutcome,
    RetractionService,
    UndoResult,
)
from backend.knowledge.domain.retraction import (
    UNDO_WINDOW_SECONDS,
    OntologyAction,
    RetractionSignal,
)
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenWriter

router = APIRouter()


# --- Schemas ---------------------------------------------------------------


class RetractRequest(BaseModel):
    """Body for ``POST /nodes/{node_ref}/retract``.

    ``correction_id`` is optional — clients that want safe-retries can
    supply a uuid they generated; the server uses it as the idempotency
    key. ``reason`` is optional founder-typed free text (per design Q2,
    low-friction; max 280 chars enforced by :class:`RetractionSignal`).
    """

    model_config = ConfigDict(extra="forbid")

    correction_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=280)


class CorrectRequest(BaseModel):
    """Body for ``POST /nodes/{node_ref}/correct``.

    Preserved for wire-compatibility, but the endpoint currently returns
    ``501 Not Implemented``: the in-place field-rewrite editor was never
    built, so accepting a ``corrections`` payload and confirming success
    would be dishonest. Retract is the only working ontology mutation.
    """

    model_config = ConfigDict(extra="forbid")

    correction_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=280)
    corrections: dict[str, str] = Field(default_factory=dict)


class RetractResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal: RetractionSignal
    created: bool
    #: Dedupe outcome — ``created`` for a fresh signal, ``already_pending`` /
    #: ``already_applied`` when an existing correction on the same node was
    #: returned instead of minting a duplicate.
    outcome: IssueOutcome = "created"
    undo_window_seconds: int = UNDO_WINDOW_SECONDS


class UndoResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correction_id: uuid.UUID
    status: Literal["undone", "expired", "already_applied", "already_undone", "not_found"]


# --- Dependencies ----------------------------------------------------------


async def build_retraction_writer(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> GardenWriter:
    """Per-workspace :class:`GardenWriter` rooted at the same vault path the
    retriever + inspector read from (FS-as-SoT). Overridable in tests via
    ``app.dependency_overrides``."""
    vault_root = _vault_root(workspace_id)
    vault_root.mkdir(parents=True, exist_ok=True)
    return GardenWriter(vault=Vault(vault_root))


async def build_retraction_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    writer: Annotated[GardenWriter, Depends(build_retraction_writer)],
) -> RetractionService:
    """Compose the application service for the request."""
    return RetractionService(session=session, writer=writer)


# --- Helpers ---------------------------------------------------------------


async def _ensure_node_exists(workspace_id: uuid.UUID, node_ref: str) -> None:
    """404 unless the referenced garden note exists in the caller's vault.

    Catches the early case so the REST surface returns the right status
    rather than persisting an orphan correction row. Path-traversal is
    blocked by :class:`FileSystemStorage._resolve`.
    """
    vault_root = _vault_root(workspace_id)
    storage = FileSystemStorage(vault_root)
    try:
        exists = await storage.exists(node_ref)
    except ValueError as exc:  # path traversal
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid node_ref"
        ) from exc
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"node not found: {node_ref}",
        )


async def _issue_with_action(
    *,
    service: RetractionService,
    session: AsyncSession,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    node_ref: str,
    action: OntologyAction,
    reason: str | None,
    correction_id: uuid.UUID | None,
    now: datetime | None = None,
) -> RetractResponse:
    """Shared intake path — issue the signal + commit."""
    signal, outcome = await service.issue(
        workspace_id=workspace_id,
        actor_id=actor_id,
        node_ref=node_ref,
        action=action,
        reason=reason,
        correction_id=correction_id,
        now=now,
    )
    await session.commit()
    return RetractResponse(signal=signal, created=outcome == "created", outcome=outcome)


# --- Endpoints -------------------------------------------------------------


@router.post("/nodes/{node_ref:path}/retract")
async def retract_node(
    node_ref: str,
    body: RetractRequest,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    user: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[RetractionService, Depends(build_retraction_service)],
) -> RetractResponse:
    """Open a retraction for a garden note. Tombstone-on-apply, undo-able 30s.

    The retract is queued — the actual frontmatter tombstone is written
    when the 30-second undo window expires (via
    :meth:`RetractionService.apply_pending` from the next call /
    background sweep). Inside the window, :func:`undo_retract_node` honors
    a cancellation and no vault write happens.

    Idempotent on ``correction_id``.
    """
    await _ensure_node_exists(workspace_id, node_ref)
    return await _issue_with_action(
        service=service,
        session=session,
        workspace_id=workspace_id,
        actor_id=user.id,
        node_ref=node_ref,
        action="retract",
        reason=body.reason,
        correction_id=body.correction_id,
    )


@router.post("/nodes/{node_ref:path}/correct")
async def correct_node(
    node_ref: str,
    body: CorrectRequest,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    _user: Annotated[UserRow, Depends(get_current_user_row)],
) -> RetractResponse:
    """Correct (in-place field rewrite) is not available yet — ``501``.

    The editor that rewrites a note's whitelisted fields was never built.
    Persisting a correction + confirming success (and later writing an
    ``ontology.correction.applied`` audit) for an operation that mutates
    nothing would be a false success + false audit record. Until the editor
    ships, this endpoint honestly reports the capability as unavailable.
    Retract (``POST /nodes/{node_ref}/retract``) is the working mutation.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="correction (in-place field rewrite) is not available yet",
    )


@router.post("/corrections/{correction_id}/undo")
async def undo_correction(
    correction_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    _user: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[RetractionService, Depends(build_retraction_service)],
) -> UndoResponse:
    """Undo a queued retraction / correction inside the 30-second window.

    Returns ``status="undone"`` on the happy path; ``status="expired"``
    when the window has already closed; ``status="already_applied"`` /
    ``"already_undone"`` / ``"not_found"`` for the corresponding terminal
    states. The endpoint is idempotent: a second undo after ``undone`` is
    ``already_undone``.
    """
    result: UndoResult = await service.undo(
        correction_id=correction_id,
        workspace_id=workspace_id,
    )
    if result == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"correction not found: {correction_id}",
        )
    await session.commit()
    return UndoResponse(correction_id=correction_id, status=result)


__all__ = ["router"]
