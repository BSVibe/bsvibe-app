"""Embedding-based intent classifier.

Implements :class:`IntentClassifierProtocol` from
:mod:`backend.gateway.rules.engine`. Pre-loaded with the account's
:class:`IntentSpec` list (id + name + per-intent threshold); embeds
the request text via an injected embedder and asks the
:class:`VectorSearchBackend` for top-K matches scoped to
``(workspace_id, account_id, embedding_model)``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import structlog

from backend.gateway.embedding.storage.backend import VectorSearchBackend

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class IntentSpec:
    """Hot-path-loaded intent metadata. Examples + embeddings live in the
    backend's index — this struct only carries what classification needs:
    id (for example→intent join), display name (the rule engine compares
    against this), and per-intent threshold."""

    id: uuid.UUID
    name: str
    threshold: float = 0.65


@runtime_checkable
class Embedder(Protocol):
    """Just the ``embed(text) -> vector`` slice of ``EmbeddingService``.

    Kept as a Protocol so tests can inject a deterministic stub without
    constructing a full :class:`EmbeddingService`.
    """

    async def embed(self, text: str) -> list[float]: ...


class _ServiceAsEmbedder:
    """Adapter: ``EmbeddingService.embed_one`` → ``embed(text) -> vector``."""

    def __init__(self, embed_fn: Callable[[str], Awaitable[list[float]]]) -> None:
        self._fn = embed_fn

    async def embed(self, text: str) -> list[float]:
        return await self._fn(text)


class IntentClassifier:
    def __init__(
        self,
        *,
        embedder: Embedder,
        backend: VectorSearchBackend,
        intents: list[IntentSpec],
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        embedding_model: str,
        top_k: int = 10,
    ) -> None:
        self._embedder = embedder
        self._backend = backend
        self._workspace_id = workspace_id
        self._account_id = account_id
        self._embedding_model = embedding_model
        self._top_k = top_k
        self._by_id: dict[uuid.UUID, IntentSpec] = {i.id: i for i in intents}

    async def classify(self, text: str) -> str | None:
        if not text or not self._by_id:
            return None

        try:
            query_vec = await self._embedder.embed(text)
        except Exception:
            logger.warning(
                "intent.embed_failed",
                exc_info=True,
                text_length=len(text),
                model=self._embedding_model,
            )
            return None

        hits = await self._backend.search(
            query=query_vec,
            workspace_id=self._workspace_id,
            account_id=self._account_id,
            embedding_model=self._embedding_model,
            limit=self._top_k,
        )
        if not hits:
            return None

        # Pick the highest-similarity hit whose intent threshold is satisfied.
        best_intent: str | None = None
        best_score: float = -1.0
        for hit in hits:
            spec = self._by_id.get(hit.entry.intent_id)
            if spec is None:
                continue
            if hit.similarity < spec.threshold:
                continue
            if hit.similarity > best_score:
                best_score = hit.similarity
                best_intent = spec.name
        if best_intent is not None:
            logger.debug("intent.classified", intent=best_intent, score=round(best_score, 4))
        return best_intent
