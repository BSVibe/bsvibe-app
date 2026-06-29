"""``POST /api/v1/inside/reindex-embeddings`` — backfill the note vector index.

The vector index (``note_embeddings``) is populated event-driven on note writes,
so bulk-imported notes and concepts (which fire no creation event) can be left
un-embedded and therefore un-retrievable. This trigger reconciles the gap for
the caller's workspace: every knowledge note (garden + concepts) lacking a
current-model vector is embedded. Idempotent — re-running is a cheap no-op.

The vault / embedder / vector-backend are built via small DI functions so tests
override them (InMemory backend + fixture vault) without a live Postgres.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.v1.decisions import _vault_root
from backend.config import get_settings
from backend.knowledge.graph.vault import Vault
from backend.knowledge.retrieval.embedder_adapter import GatewayEmbedder
from backend.knowledge.retrieval.embedder_resolution import resolve_knowledge_embedder
from backend.knowledge.retrieval.reconcile import reconcile_embeddings
from backend.knowledge.retrieval.storage.backend import NoteVectorBackend
from backend.knowledge.retrieval.storage.pg import PgNoteVectorBackend

from ._schemas import ReindexEmbeddingsResponse

router = APIRouter()


def build_inside_vault(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> Vault:
    """The caller's per-workspace vault (same root the trust ratchet writes)."""
    return Vault(_vault_root(workspace_id))


def build_inside_embedder() -> GatewayEmbedder:
    """The configured knowledge embedder (disabled → reconcile no-ops)."""
    return resolve_knowledge_embedder(get_settings())


def build_inside_vector_backend(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    embedder: Annotated[GatewayEmbedder, Depends(build_inside_embedder)],
) -> NoteVectorBackend:
    """Per-workspace pgvector note backend, stamped with the current model."""
    return PgNoteVectorBackend(
        session, workspace_id=workspace_id, embedding_model=embedder.model or ""
    )


@router.post("/reindex-embeddings")
async def reindex_embeddings(
    vault: Annotated[Vault, Depends(build_inside_vault)],
    embedder: Annotated[GatewayEmbedder, Depends(build_inside_embedder)],
    backend: Annotated[NoteVectorBackend, Depends(build_inside_vector_backend)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ReindexEmbeddingsResponse:
    """Backfill the workspace's note vector index. Embeds every knowledge note
    (garden + concepts) missing a current-model vector; idempotent."""
    result = await reconcile_embeddings(vault, embedder, backend)
    await session.commit()
    return ReindexEmbeddingsResponse(
        scanned=result.scanned,
        embedded=result.embedded,
        already=result.already,
        disabled=result.disabled,
    )


__all__ = ["router"]
