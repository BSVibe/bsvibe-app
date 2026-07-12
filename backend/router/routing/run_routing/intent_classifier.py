"""Embedding-based intent classifier (NL-native routing Lift N1).

Re-introduced from the deleted Layer-2 module (git 5c2fd55~1
``backend/router/rules/intent.py``) so run-routing rules can key on the SEMANTIC
nature of a task (``classified_intent``), not just the fixed execution-stage
callers. The embedding infra it depends on
(:class:`~backend.embedding.storage.backend.VectorSearchBackend`,
``EmbeddingService``, the ``intent_definitions`` / ``intent_examples`` tables)
survived Lift 2 — only this classify step was deleted.

Pre-loaded with the workspace's :class:`IntentSpec` list (id + name + per-intent
threshold); embeds the request text via an injected embedder and asks the vector
backend for top-K matches scoped to ``(workspace_id, account_id,
embedding_model)``. Below every intent's threshold → ``None`` (the call falls
through to other routing dimensions / the workspace default — never a silent
wrong category).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from backend.embedding.storage.backend import VectorSearchBackend

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.config import Settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class IntentSpec:
    """Hot-path-loaded intent metadata. Examples + embeddings live in the vector
    backend's index — this struct only carries what classification needs: the id
    (for the example→intent join), the display name (what a rule's
    ``classified_intent`` condition compares against), and the per-intent
    similarity threshold."""

    id: uuid.UUID
    name: str
    threshold: float = 0.65


@runtime_checkable
class Embedder(Protocol):
    """Just the ``embed(text) -> vector`` slice of ``EmbeddingService`` — kept as
    a Protocol so tests inject a deterministic stub."""

    async def embed(self, text: str) -> list[float]: ...


class ServiceAsEmbedder:
    """Adapter: an ``EmbeddingService.embed_one``-shaped coroutine → the
    :class:`Embedder` ``embed(text) -> vector`` slice."""

    def __init__(self, embed_fn: Callable[[str], Awaitable[list[float]]]) -> None:
        self._fn = embed_fn

    async def embed(self, text: str) -> list[float]:
        return await self._fn(text)


class IntentClassifier:
    """Classify request text into one of the workspace's intents, or ``None``."""

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
        except Exception:  # noqa: BLE001 — a classify hiccup must never break routing
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

        # Highest-similarity hit whose intent threshold is satisfied.
        best_intent: str | None = None
        best_score = -1.0
        for hit in hits:
            spec = self._by_id.get(hit.entry.intent_id)
            if spec is None or hit.similarity < spec.threshold:
                continue
            if hit.similarity > best_score:
                best_score = hit.similarity
                best_intent = spec.name
        if best_intent is not None:
            logger.debug("intent.classified", intent=best_intent, score=round(best_score, 4))
        return best_intent


async def build_intent_classifier(
    session: AsyncSession,
    settings: Settings,  # noqa: ARG001 — reserved for a deployment-level fallback model
    *,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
) -> IntentClassifier | None:
    """Build the workspace's classifier from surviving infra, or ``None``.

    ``None`` (a clean no-op — classified_intent stays frame-derived) when the
    account has no embedding model configured OR no intents defined yet. Two
    small indexed reads; the resolver only classifies when a rule needs it."""
    from sqlalchemy import select  # noqa: PLC0415

    from backend.embedding.db import AccountEmbeddingSettingsRow  # noqa: PLC0415
    from backend.embedding.provider import LiteLLMEmbeddingProvider  # noqa: PLC0415
    from backend.embedding.repository import IntentRepository  # noqa: PLC0415
    from backend.embedding.service import EmbeddingService  # noqa: PLC0415
    from backend.embedding.settings import EmbeddingSettings  # noqa: PLC0415
    from backend.embedding.storage.pg import PgVectorBackend  # noqa: PLC0415

    config = await session.scalar(
        select(AccountEmbeddingSettingsRow.config).where(
            AccountEmbeddingSettingsRow.workspace_id == workspace_id,
            AccountEmbeddingSettingsRow.account_id == account_id,
        )
    )
    emb_settings = EmbeddingSettings.from_account_settings(config)
    if emb_settings is None:
        return None

    rows = await IntentRepository(session).list_intents(
        workspace_id=workspace_id, account_id=account_id
    )
    if not rows:
        return None
    specs = [IntentSpec(id=r.id, name=r.name, threshold=r.threshold) for r in rows]

    service = EmbeddingService(LiteLLMEmbeddingProvider(emb_settings))

    async def _embed(text: str) -> list[float]:
        result = await service.embed_one(text)
        if result.embedding is None:
            raise RuntimeError("embedding provider returned no vector")
        return result.embedding

    return IntentClassifier(
        embedder=ServiceAsEmbedder(_embed),
        backend=PgVectorBackend(session),
        intents=specs,
        workspace_id=workspace_id,
        account_id=account_id,
        embedding_model=emb_settings.model,
    )


__all__ = [
    "Embedder",
    "IntentClassifier",
    "IntentSpec",
    "ServiceAsEmbedder",
    "build_intent_classifier",
]
