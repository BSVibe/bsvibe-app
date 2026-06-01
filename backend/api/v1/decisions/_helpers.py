"""Per-workspace vault root + canonicalization wiring for ``/api/v1/decisions``.

These three names — ``_vault_root``, ``build_canonicalization_index``,
``build_canonicalization_service`` — are re-exported from the package's
``__init__.py``. ``_vault_root`` is also imported by sibling endpoints
(``backend.api.v1.inside`` + ``backend.api.v1.checkpoints``), so the package
re-export keeps those call sites working unchanged.
"""

from __future__ import annotations

import uuid
from pathlib import Path, PurePosixPath
from typing import Annotated

from fastapi import Depends, HTTPException, status

from backend.api.deps import get_workspace_id
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


__all__ = [
    "_action_handle",
    "_ensure_addressable",
    "_vault_root",
    "build_canonicalization_index",
    "build_canonicalization_service",
]
