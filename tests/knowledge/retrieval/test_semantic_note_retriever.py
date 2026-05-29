"""G5 — GatewayEmbedder adapter + SemanticNoteRetriever.

The embedder adapter degrades to "[]" when embedding isn't configured; the
retriever embeds the signals, searches the note vector backend, and surfaces
related-note statements (similarity-floored, capped, graceful-empty, never
raises into verify). Driven against the real InMemoryNoteVectorBackend + a fake
embedder so the wiring is exercised without a live embedding provider.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.knowledge.retrieval.embedder_adapter import GatewayEmbedder
from backend.knowledge.retrieval.semantic_note_retriever import SemanticNoteRetriever
from backend.knowledge.retrieval.storage.memory import InMemoryNoteVectorBackend

pytestmark = pytest.mark.asyncio


class _FakeEmbedder:
    """Embedder Protocol stand-in mapping known texts to fixed vectors."""

    def __init__(self, vectors: dict[str, list[float]], *, enabled: bool = True) -> None:
        self._vectors = vectors
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def embed(self, text: str) -> list[float]:
        return self._vectors.get(text, [])


# --- GatewayEmbedder adapter ------------------------------------------------


async def test_gateway_embedder_disabled_when_no_service() -> None:
    emb = GatewayEmbedder(None)
    assert emb.enabled is False
    assert emb.model is None
    assert await emb.embed("anything") == []


async def test_gateway_embedder_returns_vector_from_service() -> None:
    service = SimpleNamespace(
        model="ollama/nomic-embed-text",
        embed_one=_make_embed_one([0.1, 0.2, 0.3]),
    )
    emb = GatewayEmbedder(service)  # type: ignore[arg-type]
    assert emb.enabled is True
    assert emb.model == "ollama/nomic-embed-text"
    assert await emb.embed("hello") == [0.1, 0.2, 0.3]


async def test_gateway_embedder_empty_on_provider_degradation() -> None:
    # EmbeddingService.embed_one swallows provider errors into embedding=None.
    service = SimpleNamespace(model="m", embed_one=_make_embed_one(None))
    emb = GatewayEmbedder(service)  # type: ignore[arg-type]
    assert await emb.embed("hello") == []


def _make_embed_one(vector: list[float] | None):
    async def _embed_one(text: str):
        return SimpleNamespace(text=text, embedding=vector, model="m")

    return _embed_one


# --- SemanticNoteRetriever --------------------------------------------------


async def test_disabled_embedder_yields_empty() -> None:
    backend = InMemoryNoteVectorBackend()
    await backend.store("garden/a.md", [1.0, 0.0])
    retriever = SemanticNoteRetriever(_FakeEmbedder({}, enabled=False), backend)
    assert await retriever.retrieve_for_signals("anything") == []


async def test_blank_signals_yield_empty() -> None:
    retriever = SemanticNoteRetriever(_FakeEmbedder({}), InMemoryNoteVectorBackend())
    assert await retriever.retrieve_for_signals("   ") == []


async def test_surfaces_similar_note_above_floor() -> None:
    backend = InMemoryNoteVectorBackend()
    await backend.store("garden/payments.md", [1.0, 0.0, 0.0])
    await backend.store("garden/unrelated.md", [0.0, 1.0, 0.0])
    embedder = _FakeEmbedder({"payment settings": [1.0, 0.0, 0.0]})
    retriever = SemanticNoteRetriever(embedder, backend)

    out = await retriever.retrieve_for_signals("payment settings")
    assert out == ["Related note — garden/payments.md"]  # orthogonal note filtered by floor


async def test_respects_top_k() -> None:
    backend = InMemoryNoteVectorBackend()
    for i in range(5):
        await backend.store(f"garden/n{i}.md", [1.0, 0.0])
    embedder = _FakeEmbedder({"q": [1.0, 0.0]})
    retriever = SemanticNoteRetriever(embedder, backend, top_k=2)
    assert len(await retriever.retrieve_for_signals("q")) == 2


async def test_empty_query_vector_yields_empty() -> None:
    backend = InMemoryNoteVectorBackend()
    await backend.store("garden/a.md", [1.0, 0.0])
    # The fake returns [] for an unknown text → provider degraded / disabled.
    retriever = SemanticNoteRetriever(_FakeEmbedder({}), backend)
    assert await retriever.retrieve_for_signals("unmapped") == []


async def test_never_raises_on_backend_failure() -> None:
    class _BoomBackend:
        async def store(self, note_path: str, embedding: list[float]) -> None: ...
        async def remove(self, note_path: str) -> None: ...
        async def search(self, query_embedding, top_k=10):
            raise RuntimeError("backend down")

    retriever = SemanticNoteRetriever(_FakeEmbedder({"q": [1.0]}), _BoomBackend())
    assert await retriever.retrieve_for_signals("q") == []


# --- resolver ---------------------------------------------------------------


async def test_resolve_embedder_disabled_without_account_settings() -> None:
    """No ``account_embedding_settings`` row → a disabled embedder (semantic
    search degrades to no-op rather than erroring)."""
    import uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.gateway.embedding.db import GatewayEmbeddingBase
    from backend.knowledge.retrieval.embedder_resolution import resolve_embedder
    from tests._support import db_engine

    async with db_engine(GatewayEmbeddingBase) as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as s:
            embedder = await resolve_embedder(s, workspace_id=uuid.uuid4(), account_id=uuid.uuid4())
    assert embedder.enabled is False
    assert embedder.model is None
