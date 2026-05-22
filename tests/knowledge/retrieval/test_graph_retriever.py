"""Tests for GraphRetriever — graph-based note retrieval."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.knowledge.graph.graph_models import GraphEntity, GraphRelationship
from backend.knowledge.graph.graph_store import GraphStore
from backend.knowledge.retrieval.graph_retriever import GraphRetriever


@pytest.fixture
async def store(tmp_path):
    db_path = tmp_path / ".bsage" / "graph.db"
    gs = GraphStore(db_path)
    await gs.initialize()
    yield gs
    await gs.close()


@pytest.fixture
def mock_vault(tmp_path):
    vault = MagicMock()
    vault.root = tmp_path / "vault"

    def resolve_path(path):
        return vault.root / path

    vault.resolve_path = resolve_path
    vault.read_note_content = AsyncMock(return_value="# Test Note\nSome content here.")
    return vault


async def _seed_graph(store: GraphStore):
    """Seed a small graph: BSage(project) -> Python(tool), BSage -> FastAPI(tool)."""
    bsage = GraphEntity(name="BSage", entity_type="project", source_path="garden/idea/bsage.md")
    python = GraphEntity(name="Python", entity_type="tool", source_path="garden/idea/python.md")
    fastapi = GraphEntity(name="FastAPI", entity_type="tool", source_path="garden/idea/fastapi.md")

    id_bsage = await store.upsert_entity(bsage)
    id_python = await store.upsert_entity(python)
    id_fastapi = await store.upsert_entity(fastapi)

    await store.upsert_relationship(
        GraphRelationship(
            source_id=id_bsage,
            target_id=id_python,
            rel_type="uses",
            source_path="garden/idea/bsage.md",
        )
    )
    await store.upsert_relationship(
        GraphRelationship(
            source_id=id_bsage,
            target_id=id_fastapi,
            rel_type="uses",
            source_path="garden/idea/bsage.md",
        )
    )
    return id_bsage, id_python, id_fastapi


async def test_retrieve_finds_related(store, mock_vault):
    await _seed_graph(store)
    retriever = GraphRetriever(store, mock_vault)

    result = await retriever.retrieve("BSage")
    assert "BSage" in result
    assert "Python" in result
    assert "FastAPI" in result
    assert "uses" in result


async def test_retrieve_reads_note_content(store, mock_vault):
    await _seed_graph(store)
    retriever = GraphRetriever(store, mock_vault)

    result = await retriever.retrieve("BSage")
    # Should include note content from vault
    mock_vault.read_note_content.assert_called()
    assert "Related Notes" in result


async def test_retrieve_empty_query(store, mock_vault):
    retriever = GraphRetriever(store, mock_vault)

    result = await retriever.retrieve("nonexistent entity xyz")
    assert result == ""


async def test_retrieve_max_chars_limit(store, mock_vault):
    await _seed_graph(store)
    mock_vault.read_note_content = AsyncMock(return_value="X" * 10_000)
    retriever = GraphRetriever(store, mock_vault)

    result = await retriever.retrieve("BSage", max_chars=500)
    assert len(result) <= 600  # some overhead from headers


async def test_retrieve_multi_hop(store, mock_vault):
    """Chain: A -> B -> C, querying A should find C at depth 2."""
    a = GraphEntity(name="Alpha", entity_type="concept", source_path="a.md")
    b = GraphEntity(name="Beta", entity_type="concept", source_path="b.md")
    c = GraphEntity(name="Gamma", entity_type="concept", source_path="c.md")
    id_a = await store.upsert_entity(a)
    id_b = await store.upsert_entity(b)
    id_c = await store.upsert_entity(c)

    await store.upsert_relationship(
        GraphRelationship(source_id=id_a, target_id=id_b, rel_type="related_to", source_path="a.md")
    )
    await store.upsert_relationship(
        GraphRelationship(source_id=id_b, target_id=id_c, rel_type="related_to", source_path="b.md")
    )

    retriever = GraphRetriever(store, mock_vault)
    result = await retriever.retrieve("Alpha", max_hops=2)

    # Should find Beta (hop 1) and Gamma (hop 2)
    assert "Beta" in result
    # Gamma's note should be included via multi-hop
    assert mock_vault.read_note_content.call_count >= 2


async def test_retrieve_word_matching(store, mock_vault):
    """Query words are individually matched against entity names."""
    await store.upsert_entity(
        GraphEntity(name="Python", entity_type="tool", source_path="python.md")
    )
    await store.upsert_entity(
        GraphEntity(name="FastAPI", entity_type="tool", source_path="fastapi.md")
    )

    retriever = GraphRetriever(store, mock_vault)
    result = await retriever.retrieve("Python FastAPI comparison")

    assert "Python" in result
    assert "FastAPI" in result


async def test_retrieve_handles_read_failure(store, mock_vault):
    """Read failures for individual notes don't crash the retriever."""
    await store.upsert_entity(
        GraphEntity(name="Test", entity_type="concept", source_path="missing.md")
    )
    mock_vault.read_note_content = AsyncMock(side_effect=FileNotFoundError("gone"))

    retriever = GraphRetriever(store, mock_vault)
    result = await retriever.retrieve("Test")

    # Should still return graph context even if note read fails
    assert "Test" in result
