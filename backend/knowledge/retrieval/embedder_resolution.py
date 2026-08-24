"""Resolve the knowledge-note Embedder from DEPLOYMENT settings (G6).

The pgvector note index is DERIVED from the Markdown source-of-truth (proposal
§5.4), so it must populate automatically — not be opted into per workspace. The
embedding MODEL is therefore a deployment choice (``settings.knowledge_embedding_model``).
Both seams — the settle-time population hook and the run-time retriever — resolve
their embedder from here, so every settled note is embedded against the same model
the queries use.

Since PR A the intent classifier falls back to this SAME deployment model when an
account has no per-account override (see
:func:`~backend.embedding.settings.resolve_embedding_settings`), so one deployment
runs one embedding model across both indexes. Reading it through
:meth:`EmbeddingSettings.from_deployment` keeps that a single definition rather
than two that can drift.

Disabled (``GatewayEmbedder(None)``) when ``knowledge_embedding_model`` is empty:
semantic search stays a clean no-op, canon/decision/rejection retrieval intact.
"""

from __future__ import annotations

from backend.config import Settings
from backend.embedding.provider import LiteLLMEmbeddingProvider
from backend.embedding.service import EmbeddingService
from backend.embedding.settings import EmbeddingSettings
from backend.knowledge.retrieval.embedder_adapter import GatewayEmbedder


def resolve_knowledge_embedder(settings: Settings) -> GatewayEmbedder:
    """The deployment's knowledge embedder; disabled when no model is configured.

    Pure (no DB / no per-account lookup): the model is a deployment knob, the
    note data is workspace-scoped at the storage layer
    (:class:`~backend.knowledge.retrieval.storage.pg.PgNoteVectorBackend`)."""
    embedding_settings = EmbeddingSettings.from_deployment(settings)
    if embedding_settings is None:
        return GatewayEmbedder(None)
    return GatewayEmbedder(EmbeddingService(LiteLLMEmbeddingProvider(embedding_settings)))


__all__ = ["resolve_knowledge_embedder"]
