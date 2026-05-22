"""Tests for VaultBackend (NetworkX-based graph backend)."""

from __future__ import annotations

import pytest

from backend.knowledge.graph.graph_models import (
    ConfidenceLevel,
    GraphEntity,
    GraphRelationship,
    Hyperedge,
    ProvenanceRecord,
)
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault_backend import VaultBackend


@pytest.fixture
async def backend(tmp_path):
    storage = FileSystemStorage(tmp_path / "vault")
    b = VaultBackend(storage)
    await b.initialize()
    yield b
    await b.close()


def _entity(name: str, etype: str = "person", path: str = "people/test.md") -> GraphEntity:
    return GraphEntity(name=name, entity_type=etype, source_path=path)


def _rel(src_id: str, tgt_id: str, rtype: str = "related_to") -> GraphRelationship:
    return GraphRelationship(
        source_id=src_id, target_id=tgt_id, rel_type=rtype, source_path="test.md"
    )


class TestEntityCRUD:
    async def test_upsert_entity(self, backend):
        e = _entity("Alice")
        eid = await backend.upsert_entity(e)
        assert eid == e.id
        assert await backend.count_entities() == 1

    async def test_dedup_by_name_and_type(self, backend):
        e1 = _entity("Alice")
        e2 = _entity("alice")  # Same normalized name
        id1 = await backend.upsert_entity(e1)
        id2 = await backend.upsert_entity(e2)
        assert id1 == id2
        assert await backend.count_entities() == 1

    async def test_different_types_not_deduped(self, backend):
        e1 = _entity("Python", etype="tool")
        e2 = _entity("Python", etype="concept")
        id1 = await backend.upsert_entity(e1)
        id2 = await backend.upsert_entity(e2)
        assert id1 != id2
        assert await backend.count_entities() == 2

    async def test_get_entity_by_name(self, backend):
        e = _entity("Alice")
        await backend.upsert_entity(e)
        found = await backend.get_entity_by_name("Alice")
        assert found is not None
        assert found.name == "Alice"

    async def test_get_entity_by_name_case_insensitive(self, backend):
        await backend.upsert_entity(_entity("Alice"))
        found = await backend.get_entity_by_name("alice")
        assert found is not None

    async def test_get_entity_not_found(self, backend):
        assert await backend.get_entity_by_name("Nobody") is None

    async def test_search_entities(self, backend):
        await backend.upsert_entity(_entity("Alice"))
        await backend.upsert_entity(_entity("Bob"))
        await backend.upsert_entity(_entity("Alicia", etype="concept"))
        results = await backend.search_entities("ali")
        assert len(results) == 2

    async def test_delete_by_source(self, backend):
        e = _entity("Alice", path="people/alice.md")
        await backend.upsert_entity(e)
        deleted = await backend.delete_by_source("people/alice.md")
        assert deleted >= 1
        assert await backend.count_entities() == 0

    async def test_delete_respects_provenance(self, backend):
        e = _entity("Alice", path="people/alice.md")
        eid = await backend.upsert_entity(e)
        # Add provenance from a second source
        await backend.add_provenance(
            ProvenanceRecord(
                entity_id=eid,
                source_path="notes/ref.md",
                extraction_method="rule",
                confidence=ConfidenceLevel.EXTRACTED,
                extracted_at="2026-04-19T00:00:00Z",
            )
        )
        await backend.delete_by_source("people/alice.md")
        # Entity should survive because it has provenance from another source
        assert await backend.count_entities() == 1


class TestRelationships:
    async def test_upsert_relationship(self, backend):
        e1 = _entity("Alice")
        e2 = _entity("Bob")
        id1 = await backend.upsert_entity(e1)
        id2 = await backend.upsert_entity(e2)
        rid = await backend.upsert_relationship(_rel(id1, id2))
        assert rid
        assert await backend.count_relationships() == 1

    async def test_dedup_relationship(self, backend):
        e1 = _entity("Alice")
        e2 = _entity("Bob")
        id1 = await backend.upsert_entity(e1)
        id2 = await backend.upsert_entity(e2)
        rid1 = await backend.upsert_relationship(_rel(id1, id2))
        rid2 = await backend.upsert_relationship(_rel(id1, id2))
        assert rid1 == rid2
        assert await backend.count_relationships() == 1

    async def test_query_neighbors(self, backend):
        e1 = _entity("Alice")
        e2 = _entity("Bob")
        id1 = await backend.upsert_entity(e1)
        id2 = await backend.upsert_entity(e2)
        await backend.upsert_relationship(_rel(id1, id2))
        neighbors = await backend.query_neighbors(id1)
        assert len(neighbors) == 1
        rel, ent = neighbors[0]
        assert ent.name == "Bob"

    async def test_multi_hop_query(self, backend):
        ea = _entity("A")
        eb = _entity("B")
        ec = _entity("C")
        id_a = await backend.upsert_entity(ea)
        id_b = await backend.upsert_entity(eb)
        id_c = await backend.upsert_entity(ec)
        await backend.upsert_relationship(_rel(id_a, id_b))
        await backend.upsert_relationship(_rel(id_b, id_c))
        hops = await backend.multi_hop_query(id_a, max_hops=2)
        names = {e.name for _, e in hops}
        assert "B" in names
        assert "C" in names


class TestCounts:
    async def test_count_entities_of_type(self, backend):
        await backend.upsert_entity(_entity("Alice", etype="person"))
        await backend.upsert_entity(_entity("BSage", etype="project"))
        assert await backend.count_entities_of_type("person") == 1
        assert await backend.count_entities_of_type("project") == 1

    async def test_count_relationships_for_entity(self, backend):
        e1 = _entity("Alice")
        e2 = _entity("Bob")
        id1 = await backend.upsert_entity(e1)
        id2 = await backend.upsert_entity(e2)
        await backend.upsert_relationship(_rel(id1, id2))
        count = await backend.count_relationships_for_entity("Alice")
        assert count >= 1


class TestSourceHashing:
    async def test_get_set_source_hash(self, backend):
        assert await backend.get_source_hash("test.md") is None
        await backend.set_source_hash("test.md", "abc123")
        assert await backend.get_source_hash("test.md") == "abc123"

    async def test_remove_source_hash(self, backend):
        await backend.set_source_hash("test.md", "abc123")
        await backend.remove_source_hash("test.md")
        assert await backend.get_source_hash("test.md") is None


class TestCachePersistence:
    async def test_cache_roundtrip(self, tmp_path):
        storage = FileSystemStorage(tmp_path / "vault")
        b1 = VaultBackend(storage)
        await b1.initialize()
        await b1.upsert_entity(_entity("Alice"))
        await b1.set_source_hash("test.md", "hash123")
        await b1.close()

        # Reopen — should load from cache
        b2 = VaultBackend(storage)
        await b2.initialize()
        assert await b2.count_entities() == 1
        assert await b2.get_source_hash("test.md") == "hash123"
        found = await b2.get_entity_by_name("Alice")
        assert found is not None
        await b2.close()


class TestNetworkX:
    async def test_to_networkx(self, backend):
        e1 = _entity("Alice")
        e2 = _entity("Bob")
        id1 = await backend.upsert_entity(e1)
        id2 = await backend.upsert_entity(e2)
        await backend.upsert_relationship(_rel(id1, id2))
        graph = backend.to_networkx()
        assert graph.number_of_nodes() == 2
        assert graph.number_of_edges() == 1


class TestHyperedge:
    async def test_add_hyperedge(self, backend):
        ea = _entity("Alice")
        eb = _entity("Bob")
        ec = _entity("Charlie")
        id_a = await backend.upsert_entity(ea)
        id_b = await backend.upsert_entity(eb)
        id_c = await backend.upsert_entity(ec)

        he = Hyperedge(
            name="Q1 Team",
            relation="same_team",
            members=[id_a, id_b, id_c],
            source_path="garden/hyperedges/q1-team.md",
        )
        hid = await backend.add_hyperedge(he)
        assert hid

        # Should create pairwise implicit edges (3 choose 2 = 3)
        graph = backend.to_networkx()
        # Original 0 edges + 3 pairwise
        assert graph.number_of_edges() == 3

        hyperedges = await backend.get_hyperedges()
        assert len(hyperedges) == 1
        assert hyperedges[0].relation == "same_team"


class TestConfidenceLevel:
    async def test_entity_confidence_level(self, backend):
        e = GraphEntity(
            name="Test",
            entity_type="concept",
            source_path="test.md",
            confidence=ConfidenceLevel.AMBIGUOUS,
        )
        await backend.upsert_entity(e)
        found = await backend.get_entity_by_name("Test")
        assert found is not None
        assert found.confidence == ConfidenceLevel.AMBIGUOUS
