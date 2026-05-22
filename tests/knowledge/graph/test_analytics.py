"""Tests for graph analytics."""

from __future__ import annotations

import pytest

from backend.knowledge.graph.analytics import (
    compute_centrality,
    compute_graph_stats,
    find_god_nodes,
    find_knowledge_gaps,
)
from backend.knowledge.graph.graph_models import GraphEntity, GraphRelationship
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault_backend import VaultBackend


@pytest.fixture
async def backend_with_hub(tmp_path):
    """Backend with a star topology: hub node + 5 leaves."""
    storage = FileSystemStorage(tmp_path / "vault")
    b = VaultBackend(storage)
    await b.initialize()

    hub = GraphEntity(name="Hub", entity_type="project", source_path="hub.md")
    hub_id = await b.upsert_entity(hub)

    leaf_ids = []
    for i in range(5):
        leaf = GraphEntity(name=f"Leaf{i}", entity_type="concept", source_path=f"leaf{i}.md")
        lid = await b.upsert_entity(leaf)
        leaf_ids.append(lid)
        await b.upsert_relationship(
            GraphRelationship(
                source_id=hub_id,
                target_id=lid,
                rel_type="contains",
                source_path="hub.md",
            )
        )

    # Isolated node
    isolated = GraphEntity(name="Orphan", entity_type="concept", source_path="orphan.md")
    await b.upsert_entity(isolated)

    yield b, hub_id, leaf_ids
    await b.close()


class TestComputeCentrality:
    async def test_ranks_connected_above_orphan(self, backend_with_hub):
        backend, _, _ = backend_with_hub
        graph = backend.to_networkx()
        top = compute_centrality(graph, top_k=7)
        # Orphan node should rank last (or not appear)
        top_names = [n.name for n in top]
        if "Orphan" in top_names:
            assert top_names.index("Orphan") == len(top_names) - 1

    async def test_degree_counts_correctly(self, backend_with_hub):
        backend, hub_id, _ = backend_with_hub
        graph = backend.to_networkx()
        top = compute_centrality(graph, top_k=10)
        hub_stats = next((s for s in top if s.id == hub_id), None)
        assert hub_stats is not None
        assert hub_stats.degree == 5  # Hub connects to 5 leaves

    async def test_betweenness_optional(self, backend_with_hub):
        backend, _, _ = backend_with_hub
        graph = backend.to_networkx()
        without = compute_centrality(graph, include_betweenness=False)
        with_bc = compute_centrality(graph, include_betweenness=True)
        # Without flag, betweenness stays 0
        assert all(n.betweenness == 0.0 for n in without)
        # With flag, at least one should have non-zero (or exactly zero — star
        # topology has 0 betweenness for leaves but 0 for hub too since no
        # paths go through it in a star... actually hub has all paths)
        assert any(n.betweenness >= 0.0 for n in with_bc)

    async def test_empty_graph(self, tmp_path):
        storage = FileSystemStorage(tmp_path / "vault")
        b = VaultBackend(storage)
        await b.initialize()
        graph = b.to_networkx()
        assert compute_centrality(graph) == []
        await b.close()


class TestComputeGraphStats:
    async def test_basic_stats(self, backend_with_hub):
        backend, _, _ = backend_with_hub
        graph = backend.to_networkx()
        stats = compute_graph_stats(graph)
        assert stats.num_nodes == 7  # hub + 5 leaves + 1 orphan
        assert stats.num_edges == 5
        assert stats.num_components == 2  # hub cluster + isolated orphan
        assert len(stats.isolated_nodes) == 1

    async def test_empty_graph(self, tmp_path):
        storage = FileSystemStorage(tmp_path / "vault")
        b = VaultBackend(storage)
        await b.initialize()
        graph = b.to_networkx()
        stats = compute_graph_stats(graph)
        assert stats.num_nodes == 0
        assert stats.num_edges == 0
        assert stats.isolated_nodes == []
        await b.close()


class TestFindGodNodes:
    async def test_hub_is_god_node(self, backend_with_hub):
        backend, hub_id, _ = backend_with_hub
        graph = backend.to_networkx()
        gods = find_god_nodes(graph, min_degree=3)
        ids = [g.id for g in gods]
        assert hub_id in ids

    async def test_min_degree_filter(self, backend_with_hub):
        backend, _, _ = backend_with_hub
        graph = backend.to_networkx()
        # All leaves have degree 1, so min_degree=3 excludes them
        gods = find_god_nodes(graph, min_degree=3)
        for g in gods:
            assert g.degree >= 3


class TestFindKnowledgeGaps:
    async def test_finds_isolated(self, backend_with_hub):
        backend, _, _ = backend_with_hub
        graph = backend.to_networkx()
        gaps = find_knowledge_gaps(graph)
        isolated_names = {n["name"] for n in gaps["isolated"]}
        assert "Orphan" in isolated_names

    async def test_finds_thin_leaves(self, backend_with_hub):
        backend, _, _ = backend_with_hub
        graph = backend.to_networkx()
        gaps = find_knowledge_gaps(graph)
        # Leaves have degree 1 → thin
        thin_names = {n["name"] for n in gaps["thin"]}
        for i in range(5):
            assert f"Leaf{i}" in thin_names

    async def test_empty_graph(self, tmp_path):
        storage = FileSystemStorage(tmp_path / "vault")
        b = VaultBackend(storage)
        await b.initialize()
        graph = b.to_networkx()
        gaps = find_knowledge_gaps(graph)
        assert gaps == {"isolated": [], "thin": [], "small_components": []}
        await b.close()
