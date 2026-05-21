"""Data models for the BSage knowledge graph."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


def _new_id() -> str:
    return str(uuid.uuid4())


def normalize_name(name: str) -> str:
    """Canonical form used for entity name deduplication across backends."""
    return name.strip().lower()


class ConfidenceLevel(StrEnum):
    """Extraction confidence classification.

    EXTRACTED: Rule-based extraction from frontmatter/wikilinks (high certainty).
    INFERRED: LLM-derived extraction from note body.
    AMBIGUOUS: LLM extraction with low certainty — needs human review.
               Represented as ``[[target]]?`` suffix in frontmatter.
    """

    EXTRACTED = "extracted"
    INFERRED = "inferred"
    AMBIGUOUS = "ambiguous"


class EdgeType(StrEnum):
    """Whether an edge came from frontmatter (strong) or body mention (weak)."""

    STRONG = "strong"
    WEAK = "weak"


class KnowledgeLayer(StrEnum):
    """Knowledge layer classification.

    .. deprecated::
        Replaced by bi-temporal model in Phase 2. Kept for backward compatibility
        during migration.
    """

    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"
    AFFECTIVE = "affective"


@dataclass
class GraphEntity:
    """A node in the knowledge graph.

    Attributes:
        id: Unique identifier.
        name: Human-readable entity name.
        entity_type: Ontology type (person, concept, project, tool, tag, source, etc.).
        source_path: Vault-relative path of the note that produced this entity.
        properties: Arbitrary key-value metadata.
        confidence: Extraction confidence level.
        knowledge_layer: Deprecated — kept for backward compatibility.
    """

    name: str
    entity_type: str
    source_path: str
    id: str = field(default_factory=_new_id)
    properties: dict[str, Any] = field(default_factory=dict)
    confidence: str = ConfidenceLevel.EXTRACTED
    knowledge_layer: str | None = None


@dataclass
class GraphRelationship:
    """An edge in the knowledge graph.

    Bi-temporal model (v3.1):
        valid_from/valid_to: When this fact is true in the real world.
        recorded_at: When this fact was recorded in the system.

    Attributes:
        id: Unique identifier.
        source_id: Entity ID of the source node.
        target_id: Entity ID of the target node.
        rel_type: Ontology relationship type.
        source_path: Vault-relative path of the note that produced this relationship.
        properties: Arbitrary key-value metadata.
        confidence: Extraction confidence level.
        weight: Edge importance (from ontology default_weight or calculated).
        edge_type: Strong (frontmatter) or weak (body mention).
        valid_from: ISO date when this fact became true (None = unknown).
        valid_to: ISO date when this fact stopped being true (None = still valid).
        recorded_at: ISO timestamp when this fact was recorded in the system.
    """

    source_id: str
    target_id: str
    rel_type: str
    source_path: str
    id: str = field(default_factory=_new_id)
    properties: dict[str, Any] = field(default_factory=dict)
    confidence: str = ConfidenceLevel.EXTRACTED
    weight: float = 0.5
    edge_type: str = "weak"
    valid_from: str | None = None
    valid_to: str | None = None
    recorded_at: str | None = None


@dataclass
class Hyperedge:
    """An n-ary relationship connecting 3+ entities.

    Stored as a markdown note in ``garden/hyperedges/`` with ``type: hyperedge``.
    In NetworkX, stored in ``G.graph["hyperedges"]`` and expanded into pairwise
    implicit edges between members with ``weight=implicit_weight``.

    Attributes:
        id: Unique identifier.
        name: Human-readable label for this group relationship.
        relation: The type of group relationship (e.g. same_team, co_authored).
        members: List of entity IDs participating in this relationship.
        source_path: Vault-relative path of the hyperedge note.
        properties: Arbitrary key-value metadata.
        confidence: Extraction confidence level.
        implicit_weight: Weight for pairwise implicit edges between members.
    """

    id: str = field(default_factory=_new_id)
    name: str = ""
    relation: str = "co_occurs"
    members: list[str] = field(default_factory=list)
    source_path: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    confidence: str = ConfidenceLevel.INFERRED
    implicit_weight: float = 0.3


@dataclass
class ProvenanceRecord:
    """Tracks how an entity was extracted.

    Attributes:
        entity_id: The entity this record belongs to.
        source_path: Vault-relative path of the originating note.
        extraction_method: "rule" or "llm".
        confidence: Extraction confidence level.
        extracted_at: ISO-format UTC timestamp.
    """

    entity_id: str
    source_path: str
    extraction_method: str
    confidence: str
    extracted_at: str
