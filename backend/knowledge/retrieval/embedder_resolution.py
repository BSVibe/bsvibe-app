"""Resolve a workspace/account's note Embedder from its gateway config (G5).

Both the consumption seam (the run worker's retriever) and the population seam
(the settle worker) need a :class:`~backend.knowledge.retrieval.embedder_adapter.GatewayEmbedder`
for a given workspace + account. This shared resolver reads the account's
per-account embedding config (``account_embedding_settings`` via
:class:`~backend.gateway.embedding.repository.EmbeddingSettingsRepository`) and
builds the litellm-backed service, or yields a disabled embedder when nothing is
configured — so semantic search is a clean no-op rather than an error.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.gateway.embedding.provider import build_provider
from backend.gateway.embedding.repository import EmbeddingSettingsRepository
from backend.gateway.embedding.service import EmbeddingService
from backend.knowledge.retrieval.embedder_adapter import GatewayEmbedder


async def resolve_embedder(
    session: AsyncSession, *, workspace_id: uuid.UUID, account_id: uuid.UUID
) -> GatewayEmbedder:
    """The note embedder for ``(workspace_id, account_id)``; disabled when the
    account has no embedding model configured. Never raises — a missing/odd
    config disables embedding (the consumers treat a disabled embedder as
    no-op)."""
    settings = await EmbeddingSettingsRepository(session).get(
        workspace_id=workspace_id, account_id=account_id
    )
    provider = build_provider(settings)
    service = EmbeddingService(provider) if provider is not None else None
    return GatewayEmbedder(service)


__all__ = ["resolve_embedder"]
