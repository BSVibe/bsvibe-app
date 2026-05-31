"""GatewayEmbedder — adapts the gateway EmbeddingService to the knowledge
``Embedder`` Protocol (G5).

The note vector store (G3) needs query/note embeddings; the production embedding
path is :class:`~backend.embedding.service.EmbeddingService` (litellm,
per-account model). This thin adapter wraps it so the knowledge consumers
(:class:`~backend.knowledge.retrieval.semantic_note_retriever.SemanticNoteRetriever`,
the settle-time population) depend only on the ``Embedder`` Protocol.

Degrades safely: ``enabled`` is False when no embedding model is configured for
the workspace (``service is None``); ``embed`` returns ``[]`` when disabled or
when the provider failed (``EmbeddingService.embed_one`` swallows provider errors
into ``embedding=None``). Callers treat ``[]`` as "no embedding — skip", so
semantic search is a no-op rather than an error when embedding isn't set up.
"""

from __future__ import annotations

from backend.embedding.service import EmbeddingService


class GatewayEmbedder:
    """Knowledge ``Embedder`` backed by the gateway EmbeddingService (or none)."""

    def __init__(self, service: EmbeddingService | None) -> None:
        self._service = service

    @property
    def enabled(self) -> bool:
        return self._service is not None

    @property
    def model(self) -> str | None:
        """The configured embedding model name, or ``None`` when disabled — used
        to stamp ``note_embeddings.embedding_model`` so a model swap is detectable."""
        return self._service.model if self._service is not None else None

    async def embed(self, text: str) -> list[float]:
        """The dense embedding of ``text``; ``[]`` when disabled or the provider
        degraded (never raises — the caller skips on an empty vector)."""
        if self._service is None:
            return []
        result = await self._service.embed_one(text)
        return result.embedding or []
