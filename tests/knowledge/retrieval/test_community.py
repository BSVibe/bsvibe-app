"""Tests for community detection and note generation."""

from __future__ import annotations

import pytest

from backend.knowledge.graph.graph_models import GraphEntity, GraphRelationship
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault_backend import VaultBackend
from backend.knowledge.retrieval.community import (
    Community,
    communities_to_graph_data,
    detect_communities,
    generate_community_notes,
)


@pytest.fixture
async def backend_with_graph(tmp_path):
    """Create a backend with a small graph for community testing."""
    storage = FileSystemStorage(tmp_path / "vault")
    backend = VaultBackend(storage)
    await backend.initialize()

    # Create two clusters:
    # Cluster 1: Alice - Bob - Charlie (fully connected)
    # Cluster 2: X - Y - Z (fully connected)
    # Bridge: Alice - X (weak connection)
    ids = {}
    for name in ["Alice", "Bob", "Charlie", "X", "Y", "Z"]:
        e = GraphEntity(name=name, entity_type="person", source_path=f"people/{name.lower()}.md")
        ids[name] = await backend.upsert_entity(e)

    # Cluster 1 edges
    for a, b in [("Alice", "Bob"), ("Bob", "Charlie"), ("Alice", "Charlie")]:
        await backend.upsert_relationship(
            GraphRelationship(
                source_id=ids[a],
                target_id=ids[b],
                rel_type="works_with",
                source_path="test.md",
                weight=1.0,
            )
        )

    # Cluster 2 edges
    for a, b in [("X", "Y"), ("Y", "Z"), ("X", "Z")]:
        await backend.upsert_relationship(
            GraphRelationship(
                source_id=ids[a],
                target_id=ids[b],
                rel_type="works_with",
                source_path="test.md",
                weight=1.0,
            )
        )

    # Weak bridge
    await backend.upsert_relationship(
        GraphRelationship(
            source_id=ids["Alice"],
            target_id=ids["X"],
            rel_type="knows",
            source_path="test.md",
            weight=0.1,
        )
    )

    yield backend, storage, ids
    await backend.close()


class TestDetectCommunities:
    async def test_detect_louvain(self, backend_with_graph):
        backend, _, _ = backend_with_graph
        graph = backend.to_networkx()
        communities = detect_communities(graph, algorithm="louvain")
        assert len(communities) >= 1
        total_members = sum(c.size for c in communities)
        assert total_members == 6

    async def test_detect_label_propagation(self, backend_with_graph):
        backend, _, _ = backend_with_graph
        graph = backend.to_networkx()
        communities = detect_communities(graph, algorithm="label_propagation")
        assert len(communities) >= 1

    async def test_min_size_filter(self, backend_with_graph):
        backend, _, _ = backend_with_graph
        graph = backend.to_networkx()
        communities = detect_communities(graph, min_size=4)
        # Each cluster has 3, so with min_size=4 they'd be filtered out
        # (unless merged into one big community)
        for c in communities:
            assert c.size >= 4

    async def test_empty_graph(self, tmp_path):
        storage = FileSystemStorage(tmp_path / "vault")
        backend = VaultBackend(storage)
        await backend.initialize()
        graph = backend.to_networkx()
        communities = detect_communities(graph)
        assert communities == []
        await backend.close()

    async def test_community_has_label(self, backend_with_graph):
        backend, _, _ = backend_with_graph
        graph = backend.to_networkx()
        communities = detect_communities(graph)
        for c in communities:
            assert c.label  # Auto-generated label

    async def test_community_has_cohesion(self, backend_with_graph):
        backend, _, _ = backend_with_graph
        graph = backend.to_networkx()
        communities = detect_communities(graph)
        for c in communities:
            assert 0.0 <= c.cohesion <= 1.0

    async def test_assigns_community_to_nodes(self, backend_with_graph):
        backend, _, _ = backend_with_graph
        graph = backend.to_networkx()
        detect_communities(graph)
        for _, data in graph.nodes(data=True):
            assert "community" in data


class TestGenerateCommunityNotes:
    def test_generates_markdown(self):
        comm = Community(
            id=0,
            members=["a", "b"],
            member_names=["Alice", "Bob"],
            size=2,
            label="Alice (person)",
            cohesion=1.0,
        )
        notes = generate_community_notes([comm])
        assert len(notes) == 1
        assert notes[0]["path"].startswith("garden/communities/")
        assert "type: community" in notes[0]["content"]
        assert "[[Alice]]" in notes[0]["content"]
        assert "[[Bob]]" in notes[0]["content"]

    def test_empty_communities(self):
        notes = generate_community_notes([])
        assert notes == []


class TestCommunitiesToGraphData:
    def test_format(self):
        comm = Community(
            id=0,
            members=["a", "b"],
            member_names=["Alice", "Bob"],
            size=2,
            label="Alice (person)",
            cohesion=0.75,
        )
        data = communities_to_graph_data([comm])
        assert len(data) == 1
        assert data[0]["id"] == 0
        assert data[0]["label"] == "Alice (person)"
        assert data[0]["color"].startswith("#")
        assert data[0]["members"] == ["a", "b"]
