"""Tests for LLMExtractor — LLM-based entity/relationship extraction."""

import json
from unittest.mock import AsyncMock

import pytest

from backend.knowledge.graph.graph_models import ConfidenceLevel
from backend.knowledge.ingest.llm_extractor import LLMExtractor
from backend.knowledge.retrieval.ontology import OntologyRegistry


@pytest.fixture
async def ontology(tmp_path):
    path = tmp_path / "ontology.yaml"
    registry = OntologyRegistry(path)
    await registry.load()
    return registry


@pytest.fixture
def mock_llm_fn():
    return AsyncMock()


def _llm_response(entities=None, relationships=None):
    """Build a mock LLM JSON response."""
    return json.dumps(
        {
            "entities": entities or [],
            "relationships": relationships or [],
        }
    )


async def test_extract_entities(ontology, mock_llm_fn):
    mock_llm_fn.return_value = _llm_response(
        entities=[
            {"name": "Python", "entity_type": "tool"},
            {"name": "BSage", "entity_type": "project"},
        ]
    )

    extractor = LLMExtractor(mock_llm_fn, ontology)
    entities, rels = await extractor.extract("a.md", "A" * 200)

    assert len(entities) == 2
    assert entities[0].name == "Python"
    assert entities[0].entity_type == "tool"
    assert entities[0].confidence == ConfidenceLevel.INFERRED
    assert entities[1].name == "BSage"
    assert entities[1].entity_type == "project"


async def test_extract_relationships(ontology, mock_llm_fn):
    # v2.2: "uses" has domain=person, range=[tool, concept]
    mock_llm_fn.return_value = _llm_response(
        entities=[
            {"name": "Alice", "entity_type": "person"},
            {"name": "Python", "entity_type": "tool"},
        ],
        relationships=[
            {"source": "Alice", "target": "Python", "rel_type": "uses"},
        ],
    )

    extractor = LLMExtractor(mock_llm_fn, ontology)
    entities, rels = await extractor.extract("a.md", "B" * 200)

    assert len(rels) == 1
    assert rels[0].rel_type == "uses"
    assert rels[0].confidence == ConfidenceLevel.INFERRED


async def test_free_form_entity_types_accepted(ontology, mock_llm_fn):
    """Post dynamic-ontology refactor entity types are FREE-FORM strings.

    The static enum / fall-back-to-concept behaviour is gone — whatever
    the LLM produces survives, normalised to lowercase. Identity in the
    graph comes from connections, not from a curated type list."""
    mock_llm_fn.return_value = _llm_response(
        entities=[{"name": "Foo", "entity_type": "newly_invented_kind"}]
    )

    extractor = LLMExtractor(mock_llm_fn, ontology)
    entities, _ = await extractor.extract("a.md", "C" * 200)

    assert entities[0].entity_type == "newly_invented_kind"


async def test_invalid_rel_type_falls_back(ontology, mock_llm_fn):
    """Unknown relationship type falls back to 'related_to'."""
    mock_llm_fn.return_value = _llm_response(
        entities=[
            {"name": "A", "entity_type": "concept"},
            {"name": "B", "entity_type": "concept"},
        ],
        relationships=[{"source": "A", "target": "B", "rel_type": "invented_rel"}],
    )

    extractor = LLMExtractor(mock_llm_fn, ontology)
    _, rels = await extractor.extract("a.md", "D" * 200)

    assert rels[0].rel_type == "related_to"


async def test_short_body_skipped(ontology, mock_llm_fn):
    """Body text shorter than threshold is skipped."""
    extractor = LLMExtractor(mock_llm_fn, ontology)
    entities, rels = await extractor.extract("a.md", "Short text")

    assert entities == []
    assert rels == []
    mock_llm_fn.assert_not_awaited()


async def test_dedup_by_content_hash(ontology, mock_llm_fn):
    """Same content for same path is not re-extracted."""
    mock_llm_fn.return_value = _llm_response(entities=[{"name": "X", "entity_type": "concept"}])
    body = "E" * 200

    extractor = LLMExtractor(mock_llm_fn, ontology)
    e1, _ = await extractor.extract("a.md", body)
    e2, _ = await extractor.extract("a.md", body)

    assert len(e1) == 1
    assert len(e2) == 0  # deduped
    assert mock_llm_fn.await_count == 1


async def test_different_content_not_deduped(ontology, mock_llm_fn):
    """Different content for same path IS extracted."""
    mock_llm_fn.return_value = _llm_response(entities=[{"name": "X", "entity_type": "concept"}])

    extractor = LLMExtractor(mock_llm_fn, ontology)
    await extractor.extract("a.md", "F" * 200)
    await extractor.extract("a.md", "G" * 200)

    assert mock_llm_fn.await_count == 2


async def test_llm_failure_returns_empty(ontology, mock_llm_fn):
    """LLM call failure returns empty results."""
    mock_llm_fn.side_effect = RuntimeError("LLM down")

    extractor = LLMExtractor(mock_llm_fn, ontology)
    entities, rels = await extractor.extract("a.md", "H" * 200)

    assert entities == []
    assert rels == []


async def test_invalid_json_returns_empty(ontology, mock_llm_fn):
    """Invalid JSON response returns empty results."""
    mock_llm_fn.return_value = "not valid json"

    extractor = LLMExtractor(mock_llm_fn, ontology)
    entities, rels = await extractor.extract("a.md", "I" * 200)

    assert entities == []
    assert rels == []


async def test_markdown_code_block_response(ontology, mock_llm_fn):
    """Handle LLM response wrapped in markdown code blocks."""
    response = (
        "```json\n" + _llm_response(entities=[{"name": "Test", "entity_type": "concept"}]) + "\n```"
    )
    mock_llm_fn.return_value = response

    extractor = LLMExtractor(mock_llm_fn, ontology)
    entities, _ = await extractor.extract("a.md", "J" * 200)

    assert len(entities) == 1
    assert entities[0].name == "Test"


async def test_relationship_with_unknown_entity_skipped(ontology, mock_llm_fn):
    """Relationships referencing unknown entities are skipped."""
    mock_llm_fn.return_value = _llm_response(
        entities=[{"name": "A", "entity_type": "concept"}],
        relationships=[{"source": "A", "target": "Unknown", "rel_type": "uses"}],
    )

    extractor = LLMExtractor(mock_llm_fn, ontology)
    _, rels = await extractor.extract("a.md", "K" * 200)

    assert len(rels) == 0  # target "Unknown" not in entity list


# Auto-evolution of entity types was removed in the dynamic-ontology
# refactor — entity types are free-form strings, so there's nothing to
# evolve. Relation-type auto-evolution survives below.


# ---------------------------------------------------------------------------
# LRU cache tests
# ---------------------------------------------------------------------------


async def test_processed_hashes_lru_evicts_oldest(ontology, mock_llm_fn):
    """When cache exceeds MAX_CACHE_SIZE, oldest entries are evicted."""
    from backend.knowledge.ingest.llm_extractor import _MAX_CACHE_SIZE

    extractor = LLMExtractor(mock_llm_fn, ontology)
    mock_llm_fn.return_value = _llm_response()

    # Fill cache to max
    for i in range(_MAX_CACHE_SIZE + 5):
        await extractor.extract(f"note{i}.md", f"{'X' * 200} unique {i}")

    assert len(extractor._processed_hashes) <= _MAX_CACHE_SIZE


async def test_processed_hashes_lru_keeps_recent(ontology, mock_llm_fn):
    """Recently accessed entries survive eviction."""
    extractor = LLMExtractor(mock_llm_fn, ontology)
    mock_llm_fn.return_value = _llm_response()

    # Insert two entries
    body_a = "A" * 200
    body_b = "B" * 200
    await extractor.extract("a.md", body_a)
    await extractor.extract("b.md", body_b)

    # Re-access a.md to move it to end (most recent)
    entities, _ = await extractor.extract("a.md", body_a)
    assert entities == []  # cache hit returns empty

    # Verify a.md is still in cache (moved to end)
    import hashlib

    hash_a = hashlib.sha256(body_a.encode()).hexdigest()[:16]
    assert f"a.md:{hash_a}" in extractor._processed_hashes


# ---------------------------------------------------------------------------
# Relationship type auto-evolution tests
# ---------------------------------------------------------------------------


async def test_auto_evolve_relationship_type_after_threshold(ontology, mock_llm_fn):
    """When auto_evolve=True and unknown rel_type appears >= threshold times, it is added."""
    extractor = LLMExtractor(mock_llm_fn, ontology, auto_evolve=True)
    extractor._unknown_threshold = 2

    assert not ontology.is_valid_relationship_type("mentors")

    for i in range(2):
        mock_llm_fn.return_value = _llm_response(
            entities=[
                {"name": f"PersonA{i}", "entity_type": "person"},
                {"name": f"PersonB{i}", "entity_type": "person"},
            ],
            relationships=[
                {"source": f"PersonA{i}", "target": f"PersonB{i}", "rel_type": "mentors"},
            ],
        )
        await extractor.extract(f"note{i}.md", f"{'R' * 200}{i}")

    assert ontology.is_valid_relationship_type("mentors")


async def test_auto_evolve_relationship_disabled_by_default(ontology, mock_llm_fn):
    """When auto_evolve=False (default), unknown rel_types are not added."""
    extractor = LLMExtractor(mock_llm_fn, ontology)

    for i in range(5):
        mock_llm_fn.return_value = _llm_response(
            entities=[
                {"name": f"A{i}", "entity_type": "concept"},
                {"name": f"B{i}", "entity_type": "concept"},
            ],
            relationships=[
                {"source": f"A{i}", "target": f"B{i}", "rel_type": "mentors"},
            ],
        )
        await extractor.extract(f"note{i}.md", f"{'S' * 200}{i}")

    assert not ontology.is_valid_relationship_type("mentors")


async def test_auto_evolve_rel_type_uses_original_after_evolution(ontology, mock_llm_fn):
    """After auto-evolving a rel_type, subsequent extractions use the original type."""
    extractor = LLMExtractor(mock_llm_fn, ontology, auto_evolve=True)
    extractor._unknown_threshold = 1

    mock_llm_fn.return_value = _llm_response(
        entities=[
            {"name": "Alice", "entity_type": "person"},
            {"name": "Bob", "entity_type": "person"},
        ],
        relationships=[
            {"source": "Alice", "target": "Bob", "rel_type": "supervises"},
        ],
    )
    await extractor.extract("note0.md", "T" * 200)

    assert ontology.is_valid_relationship_type("supervises")

    mock_llm_fn.return_value = _llm_response(
        entities=[
            {"name": "Carol", "entity_type": "person"},
            {"name": "Dave", "entity_type": "person"},
        ],
        relationships=[
            {"source": "Carol", "target": "Dave", "rel_type": "supervises"},
        ],
    )
    _, rels = await extractor.extract("note1.md", "U" * 200)
    assert rels[0].rel_type == "supervises"
