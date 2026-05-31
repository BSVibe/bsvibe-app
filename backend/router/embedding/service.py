"""Embed lifecycle — generate + tag with model + graceful degrade.

Per-account scoped (constructed against the account's
:class:`EmbeddingSettings`). All provider failures degrade to
``embedding=None`` so example creation never blocks on a transient API
outage. Stale rows surface later via
``IntentRepository.list_examples_needing_reembedding``.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from backend.router.embedding.provider import EmbeddingProvider

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EmbeddedExample:
    """One ``embed`` result. ``embedding=None`` ⇒ provider failed."""

    text: str
    embedding: list[float] | None
    model: str


class EmbeddingService:
    def __init__(self, provider: EmbeddingProvider) -> None:
        self._provider = provider

    @property
    def model(self) -> str:
        return self._provider.model

    async def test_connection(self) -> int:
        """Returns the embedding dimension on success.

        Lets ``account_embedding_settings`` validation reject configs
        whose provider is unreachable before any intent example is
        written.
        """
        result = await self._provider.embed(["ping"])
        if not result or not result[0]:
            raise RuntimeError("Embedding provider returned empty result for test input")
        return len(result[0])

    async def embed_one(self, text: str) -> EmbeddedExample:
        try:
            vectors = await self._provider.embed([text])
        except Exception:
            logger.warning(
                "embedding.generation_failed",
                exc_info=True,
                text_length=len(text),
                model=self._provider.model,
            )
            return EmbeddedExample(text=text, embedding=None, model=self._provider.model)
        return EmbeddedExample(text=text, embedding=vectors[0], model=self._provider.model)

    async def embed_many(self, texts: list[str]) -> list[EmbeddedExample]:
        if not texts:
            return []
        try:
            vectors = await self._provider.embed(texts)
        except Exception:
            logger.warning(
                "embedding.batch_failed",
                exc_info=True,
                count=len(texts),
                model=self._provider.model,
            )
            return [
                EmbeddedExample(text=t, embedding=None, model=self._provider.model) for t in texts
            ]
        return [
            EmbeddedExample(text=t, embedding=v, model=self._provider.model)
            for t, v in zip(texts, vectors, strict=True)
        ]
