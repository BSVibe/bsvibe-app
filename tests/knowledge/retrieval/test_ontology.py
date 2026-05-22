"""Tests for OntologyRegistry (schema v4 — dynamic-ontology refactor).

Schema v4 removed ``entity_types`` entirely (identity comes from tags +
entities + community, not a static enum). The registry still owns
``relation_types`` (typed graph edges) and ``evolution_config``.
"""

from __future__ import annotations

import pytest
import yaml

from backend.knowledge.retrieval.ontology import OntologyRegistry


@pytest.mark.asyncio
async def test_load_creates_default(tmp_path):
    path = tmp_path / ".bsage" / "ontology.yaml"
    registry = OntologyRegistry(path)
    await registry.load()

    assert path.exists()
    # Schema v4 — entity_types are gone, relation_types survive.
    assert registry.schema_version == 4
    assert registry.is_valid_relationship_type("related_to")
    assert registry.is_valid_relationship_type("references")
    assert registry.is_valid_relationship_type("attendees")
    assert registry.is_valid_relationship_type("supersedes")


@pytest.mark.asyncio
async def test_load_existing_file_keeps_user_relations(tmp_path):
    path = tmp_path / "ontology.yaml"
    data = {
        "schema_version": 99,
        "relation_types": {"custom_rel": {"description": "A custom rel", "default_weight": 0.5}},
    }
    with open(path, "w") as f:
        yaml.dump(data, f)

    registry = OntologyRegistry(path)
    await registry.load()

    assert registry.schema_version == 99
    assert registry.is_valid_relationship_type("custom_rel")
    assert not registry.is_valid_relationship_type("related_to")


@pytest.mark.asyncio
async def test_default_ontology_has_no_entity_types_key(tmp_path):
    """Schema v4 ships without an ``entity_types`` key. Vaults that have
    one in their YAML keep it (back-compat) but the registry never reads
    it for routing."""
    path = tmp_path / "ontology.yaml"
    registry = OntologyRegistry(path)
    await registry.load()

    raw = yaml.safe_load(path.read_text())
    assert "entity_types" not in raw
    assert "relation_types" in raw


@pytest.mark.asyncio
async def test_get_relation_types(tmp_path):
    path = tmp_path / "ontology.yaml"
    registry = OntologyRegistry(path)
    await registry.load()

    types = registry.get_relation_types()
    assert "related_to" in types
    assert "default_weight" in types["related_to"]
    assert "inverse" in types["related_to"]


@pytest.mark.asyncio
async def test_relation_types_have_required_fields(tmp_path):
    path = tmp_path / "ontology.yaml"
    registry = OntologyRegistry(path)
    await registry.load()

    for rtype in (
        "related_to",
        "references",
        "tagged_with",
        "belongs_to",
        "attends",
        "attendees",
        "supersedes",
    ):
        assert registry.is_valid_relationship_type(rtype), f"Missing relation type: {rtype}"
        rel_info = registry.get_relation_types()[rtype]
        assert "default_weight" in rel_info
        assert "inverse" in rel_info


@pytest.mark.asyncio
async def test_add_relationship_type(tmp_path):
    path = tmp_path / "ontology.yaml"
    registry = OntologyRegistry(path)
    await registry.load()

    added = await registry.add_relationship_type("collaborates_with", "Two people working together")
    assert added is True
    assert registry.is_valid_relationship_type("collaborates_with")


@pytest.mark.asyncio
async def test_add_relationship_type_duplicate(tmp_path):
    path = tmp_path / "ontology.yaml"
    registry = OntologyRegistry(path)
    await registry.load()

    added = await registry.add_relationship_type("related_to", "duplicate")
    assert added is False


@pytest.mark.asyncio
async def test_validate_relationship_type_fallback(tmp_path):
    path = tmp_path / "ontology.yaml"
    registry = OntologyRegistry(path)
    await registry.load()

    assert registry.validate_relationship_type("related_to") == "related_to"
    assert registry.validate_relationship_type("totally_made_up") == "related_to"


@pytest.mark.asyncio
async def test_get_relation_weight(tmp_path):
    path = tmp_path / "ontology.yaml"
    registry = OntologyRegistry(path)
    await registry.load()

    assert 0.0 <= registry.get_relation_weight("related_to") <= 1.0
    # Unknown rel falls back to 0.5.
    assert registry.get_relation_weight("totally_made_up") == 0.5


@pytest.mark.asyncio
async def test_get_inverse(tmp_path):
    path = tmp_path / "ontology.yaml"
    registry = OntologyRegistry(path)
    await registry.load()

    assert registry.get_inverse("related_to") == "related_to"
    assert registry.get_inverse("references") == "referenced_by"
    assert registry.get_inverse("totally_made_up") is None


@pytest.mark.asyncio
async def test_evolution_config(tmp_path):
    path = tmp_path / "ontology.yaml"
    registry = OntologyRegistry(path)
    await registry.load()

    cfg = registry.get_evolution_config()
    assert isinstance(cfg, dict)
    assert "create_threshold" in cfg


@pytest.mark.asyncio
async def test_version_property_compat(tmp_path):
    path = tmp_path / "ontology.yaml"
    registry = OntologyRegistry(path)
    await registry.load()

    # ``version`` is the legacy str alias of schema_version.
    assert registry.version == "4"
    assert registry.schema_version == 4
