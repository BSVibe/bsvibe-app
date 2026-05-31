"""Embedder protocol — minimal surface VaultRetriever / VectorSubscriber need.

BSage's concrete ``Embedder`` is dropped in favor of structural typing. Wire-time
adapters can wrap ``backend.embedding.EmbeddingService`` (or any other
embedding provider) so long as the adapter implements this Protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """Embedding provider used by retrieval / vector subscribers."""

    @property
    def enabled(self) -> bool:
        """Whether the embedder is operational; False to short-circuit callers."""
        ...

    async def embed(self, text: str) -> list[float]:
        """Embed a single string. Should return ``[]`` on disabled/failure."""
        ...
