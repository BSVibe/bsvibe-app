"""The ingest compiler's eyes — a semantic view of a workspace's existing notes.

:class:`~backend.knowledge.ingest.ingest_compiler.IngestCompiler` calls
:func:`~backend.knowledge.ingest.ingest_compiler._related_context.find_related`
ONCE PER CHUNK to decide create-vs-update. With no retriever that call
short-circuits to ``"No existing notes available."`` — so every production
ingest and settle compile has decided against nothing.

The structure audit (2026-08-19) read this as "two call sites forgot an
argument". It was a layer worse: ``VaultRetriever`` had **zero** production
construction sites (tests only), as did ``FileIndexReader`` and
인덱스 구독자 — the vault ``_index`` was never written. There was no
object to forget to pass.

What DOES exist, and is current, is the semantic store: prod measured 1,714
``note_embeddings`` rows across 2 workspaces, freshest 2026-08-19, kept up by
``reconcile_embeddings`` from these same two runtimes. This module hands the
compiler that.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from backend.config import Settings
    from backend.knowledge.retrieval.retriever import VaultRetriever


def build_ingest_retriever(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None,
    region: str,
    workspace_id: uuid.UUID,
) -> VaultRetriever | None:
    """A workspace-scoped semantic note retriever, or ``None``.

    ``None`` when no embedding model is configured (or no session factory reached
    this caller) — deliberately, and NOT a bare
    ``VaultRetriever(vault)``. With no vector store that falls back to RECENCY,
    which answers a different question: it returns the most RECENT notes, not the
    related ones. Handing those to a chunk as its "existing related notes" invites
    an update against a note that has nothing to do with it — worse than honest
    silence. The same guard (``embedder.enabled``) already gates the sibling
    ``reconcile_embeddings`` hooks in both runtimes.
    """
    from backend.knowledge.graph.vault import Vault  # noqa: PLC0415
    from backend.knowledge.graph.vault_paths import (  # noqa: PLC0415
        workspace_vault_root,
    )
    from backend.knowledge.retrieval.embedder_resolution import (  # noqa: PLC0415
        resolve_knowledge_embedder,
    )
    from backend.knowledge.retrieval.retriever import VaultRetriever  # noqa: PLC0415
    from backend.knowledge.retrieval.storage.pg import (  # noqa: PLC0415
        SessionScopedNoteVectorBackend,
    )

    if session_factory is None:
        # No DB handle reached here, so there is nothing to search. Silence is
        # the honest answer; a recency fallback would invent relatedness.
        return None
    embedder = resolve_knowledge_embedder(settings)
    if not embedder.enabled or embedder.model is None:
        return None
    return VaultRetriever(
        Vault(workspace_vault_root(workspace_id)),
        # Session-SCOPED on purpose: the compiler searches from CONCURRENT chunk
        # tasks and one AsyncSession is not safe for concurrent use — the same
        # E18/E19 reasoning both runtimes already carry for their LLM adapters.
        vector_store=SessionScopedNoteVectorBackend(
            session_factory, workspace_id=workspace_id, embedding_model=embedder.model
        ),
        embedder=embedder,
    )


__all__ = ["build_ingest_retriever"]
