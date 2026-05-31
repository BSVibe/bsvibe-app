"""Resolve the knowledge-note Embedder from DEPLOYMENT settings (G6).

The pgvector note index is DERIVED from the Markdown source-of-truth (proposal
§5.4), so it must populate automatically — not be opted into per workspace. The
embedding MODEL is therefore a deployment choice (``settings.knowledge_embedding_model``),
not the gateway's per-account intent-routing config. Both seams — the settle-time
population hook and the run-time retriever — resolve their embedder from here, so
every settled note is embedded against the same model the queries use.

Disabled (``GatewayEmbedder(None)``) when ``knowledge_embedding_model`` is empty:
semantic search stays a clean no-op, canon/decision/rejection retrieval intact.
"""

from __future__ import annotations

from backend.config import Settings
from backend.knowledge.retrieval.embedder_adapter import GatewayEmbedder
from backend.router.embedding.provider import LiteLLMEmbeddingProvider
from backend.router.embedding.service import EmbeddingService
from backend.router.embedding.settings import EmbeddingSettings


def resolve_knowledge_embedder(settings: Settings) -> GatewayEmbedder:
    """The deployment's knowledge embedder; disabled when no model is configured.

    Pure (no DB / no per-account lookup): the model is a deployment knob, the
    note data is workspace-scoped at the storage layer
    (:class:`~backend.knowledge.retrieval.storage.pg.PgNoteVectorBackend`)."""
    model = (settings.knowledge_embedding_model or "").strip()
    if not model:
        return GatewayEmbedder(None)
    embedding_settings = EmbeddingSettings(
        model=model,
        api_base=settings.knowledge_embedding_api_base,
        timeout=settings.knowledge_embedding_timeout_s,
    )
    return GatewayEmbedder(EmbeddingService(LiteLLMEmbeddingProvider(embedding_settings)))


__all__ = ["resolve_knowledge_embedder"]
