"""Tests for graph_models dataclasses."""

from backend.knowledge.graph.graph_models import (
    ConfidenceLevel,
    GraphEntity,
    GraphRelationship,
    ProvenanceRecord,
)


def test_graph_entity_defaults():
    e = GraphEntity(name="BSage", entity_type="project", source_path="garden/idea/bsage.md")
    assert e.name == "BSage"
    assert e.entity_type == "project"
    assert e.source_path == "garden/idea/bsage.md"
    assert e.confidence == ConfidenceLevel.EXTRACTED
    assert e.knowledge_layer is None
    assert e.properties == {}
    assert e.id  # auto-generated UUID


def test_graph_entity_custom_fields():
    e = GraphEntity(
        name="Alice",
        entity_type="person",
        source_path="garden/idea/meeting.md",
        id="custom-id",
        properties={"role": "engineer"},
        confidence=ConfidenceLevel.INFERRED,
    )
    assert e.id == "custom-id"
    assert e.properties["role"] == "engineer"
    assert e.confidence == ConfidenceLevel.INFERRED


def test_graph_entity_unique_ids():
    e1 = GraphEntity(name="A", entity_type="concept", source_path="a.md")
    e2 = GraphEntity(name="B", entity_type="concept", source_path="b.md")
    assert e1.id != e2.id


def test_graph_relationship_defaults():
    r = GraphRelationship(source_id="s1", target_id="t1", rel_type="related_to", source_path="a.md")
    assert r.source_id == "s1"
    assert r.target_id == "t1"
    assert r.rel_type == "related_to"
    assert r.confidence == ConfidenceLevel.EXTRACTED
    assert r.id  # auto-generated


def test_provenance_record():
    p = ProvenanceRecord(
        entity_id="e1",
        source_path="garden/idea/x.md",
        extraction_method="rule",
        confidence=ConfidenceLevel.EXTRACTED,
        extracted_at="2026-03-12T00:00:00Z",
    )
    assert p.extraction_method == "rule"
    assert p.confidence == ConfidenceLevel.EXTRACTED
