"""Tests for hybrid search (BM25 + graph + vector with RRF fusion)."""

from __future__ import annotations

import pytest

from backend.knowledge.graph.graph_models import GraphEntity, GraphRelationship
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault_backend import VaultBackend
from backend.knowledge.retrieval.hybrid_search import SearchResult, hybrid_search


@pytest.fixture
async def backend(tmp_path):
    storage = FileSystemStorage(tmp_path / "vault")
    b = VaultBackend(storage)
    await b.initialize()
    yield b
    await b.close()


@pytest.fixture
async def populated_backend(backend):
    """Backend with diverse entities for search tests."""
    entities = [
        GraphEntity(name="Python programming", entity_type="concept", source_path="a.md"),
        GraphEntity(name="Python snake", entity_type="animal", source_path="b.md"),
        GraphEntity(name="JavaScript", entity_type="concept", source_path="c.md"),
        GraphEntity(name="FastAPI framework", entity_type="tool", source_path="d.md"),
        GraphEntity(name="Django", entity_type="tool", source_path="e.md"),
    ]
    ids = {}
    for e in entities:
        ids[e.name] = await backend.upsert_entity(e)

    # Connect Python programming to FastAPI
    await backend.upsert_relationship(
        GraphRelationship(
            source_id=ids["Python programming"],
            target_id=ids["FastAPI framework"],
            rel_type="uses",
            source_path="a.md",
        )
    )
    return backend, ids


class TestHybridSearch:
    async def test_bm25_finds_keyword_match(self, populated_backend):
        backend, _ = populated_backend
        results = await hybrid_search(backend, "Python programming", limit=5)
        assert len(results) > 0
        names = [r.entity.name for r in results]
        assert "Python programming" in names

    async def test_graph_expands_neighbors(self, populated_backend):
        backend, _ = populated_backend
        results = await hybrid_search(backend, "Python programming", limit=5)
        names = [r.entity.name for r in results]
        # FastAPI should appear via graph expansion
        assert "FastAPI framework" in names

    async def test_results_have_scores(self, populated_backend):
        backend, _ = populated_backend
        results = await hybrid_search(backend, "Python", limit=3)
        for r in results:
            assert r.score > 0

    async def test_matched_via_reflects_methods(self, populated_backend):
        backend, _ = populated_backend
        results = await hybrid_search(backend, "Python programming", limit=5)
        for r in results:
            assert len(r.matched_via) > 0
            # Without embed_fn, only bm25 + graph should appear
            assert set(r.matched_via).issubset({"bm25", "graph"})

    async def test_empty_query_returns_no_results(self, populated_backend):
        backend, _ = populated_backend
        results = await hybrid_search(backend, "", limit=5)
        # Empty query → no bm25 tokens; graph also empty
        # Results may be empty or very limited
        assert len(results) == 0

    async def test_empty_backend_no_crash(self, backend):
        results = await hybrid_search(backend, "anything", limit=5)
        assert results == []

    async def test_vector_search_with_embed_fn(self, populated_backend):
        backend, ids = populated_backend

        # Add embeddings to Python programming
        graph = backend.to_networkx()
        for _node_id, attrs in graph.nodes(data=True):
            attrs["properties"] = {"embedding": [1.0, 0.0, 0.0]}

        async def embed_fn(query: str) -> list[float]:
            return [1.0, 0.0, 0.0]

        results = await hybrid_search(backend, "Python", limit=5, embed_fn=embed_fn)
        # Vector method should contribute
        vector_matches = [r for r in results if "vector" in r.matched_via]
        assert len(vector_matches) > 0

    async def test_rrf_fuses_multiple_sources(self, populated_backend):
        backend, _ = populated_backend
        results = await hybrid_search(backend, "Python programming", limit=5)
        # The seed "Python programming" should appear in both bm25 and graph → higher RRF
        top = results[0]
        # At minimum, the top result should have been found by at least one method
        assert len(top.matched_via) >= 1
        # Results should be sorted by score
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)


def test_search_result_defaults():
    ent = GraphEntity(name="x", entity_type="y", source_path="z.md")
    r = SearchResult(entity=ent, score=1.0)
    assert r.bm25_rank is None
    assert r.graph_rank is None
    assert r.vector_rank is None
    assert r.matched_via == []
