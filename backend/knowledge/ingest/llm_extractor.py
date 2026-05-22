"""LLMExtractor — LLM-based entity/relationship extraction constrained by ontology."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from string import Template
from typing import TYPE_CHECKING

import structlog

from backend.knowledge.graph.graph_models import ConfidenceLevel, GraphEntity, GraphRelationship

if TYPE_CHECKING:
    from backend.knowledge.retrieval.ontology import OntologyRegistry

logger = structlog.get_logger(__name__)

_MIN_BODY_CHARS = 100

_MAX_CACHE_SIZE = 10_000

_SYSTEM_PROMPT_TEMPLATE = Template("""\
You are a knowledge graph extraction assistant.
Extract entities and relationships from the given text.

GUIDELINES:
- Entity types are FREE-FORM lowercase strings describing what the entity
  IS (e.g. "person", "tool", "company", "concept"). Use whatever fits;
  consistency emerges via downstream tag normalisation.
- Relationship types should come from this curated list when one fits:
  $relationship_types. If none fits, use "related_to".
- Only extract clearly stated facts, not speculation.

Respond with ONLY valid JSON in this format:
{
  "entities": [
    {"name": "entity name", "entity_type": "type", "properties": {}}
  ],
  "relationships": [
    {"source": "entity name", "target": "entity name", "rel_type": "type"}
  ]
}

If no entities or relationships can be extracted, respond with:
{"entities": [], "relationships": []}""")


class LLMExtractor:
    """Extracts entities and relationships from unstructured text using an LLM.

    The ontology schema constrains what types the LLM can produce,
    preventing hallucinated entity/relationship types.
    All LLM-extracted items have ``confidence=ConfidenceLevel.INFERRED``.
    """

    def __init__(
        self,
        llm_fn: Callable[[str, str], Awaitable[str]],
        ontology: OntologyRegistry,
        *,
        auto_evolve: bool = False,
    ) -> None:
        self._llm_fn = llm_fn
        self._ontology = ontology
        self._auto_evolve = auto_evolve
        self._processed_hashes: OrderedDict[str, None] = OrderedDict()
        self._unknown_type_counts: dict[str, int] = {}
        self._unknown_rel_type_counts: dict[str, int] = {}
        # Read threshold from ontology evolution_config, fallback to 3
        evo_config = ontology.get_evolution_config()
        self._unknown_threshold: int = int(evo_config.get("create_threshold", 3))

    async def extract(
        self, rel_path: str, body_text: str
    ) -> tuple[list[GraphEntity], list[GraphRelationship]]:
        """Extract entities and relationships from note body text.

        Args:
            rel_path: Vault-relative path of the note.
            body_text: The body text (after frontmatter) to extract from.

        Returns:
            Tuple of (entities, relationships). Empty if body too short
            or already processed with same content.
        """
        if len(body_text.strip()) < _MIN_BODY_CHARS:
            return [], []

        # Deduplicate by content hash
        content_hash = hashlib.sha256(body_text.encode()).hexdigest()[:16]
        cache_key = f"{rel_path}:{content_hash}"
        if cache_key in self._processed_hashes:
            self._processed_hashes.move_to_end(cache_key)
            return [], []
        self._processed_hashes[cache_key] = None
        if len(self._processed_hashes) > _MAX_CACHE_SIZE:
            self._processed_hashes.popitem(last=False)

        # Build prompt — only relationship types are constrained; entity
        # types went free-form in the dynamic-ontology refactor.
        relationship_types = ", ".join(self._ontology.get_relationship_types().keys())
        system = _SYSTEM_PROMPT_TEMPLATE.safe_substitute(
            relationship_types=relationship_types,
        )

        try:
            response = await self._llm_fn(system, body_text)
            return await self._parse_response(response, rel_path)
        except (json.JSONDecodeError, ValueError, TypeError, RuntimeError, OSError):
            logger.warning("llm_extraction_failed", path=rel_path, exc_info=True)
            return [], []

    async def _parse_response(
        self, response: str, rel_path: str
    ) -> tuple[list[GraphEntity], list[GraphRelationship]]:
        """Parse LLM JSON response into graph objects."""
        try:
            # Handle markdown code blocks
            text = response.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("llm_extraction_parse_failed", path=rel_path)
            return [], []

        entities: list[GraphEntity] = []
        relationships: list[GraphRelationship] = []
        entity_name_map: dict[str, str] = {}  # name -> entity id

        for raw in data.get("entities", []):
            name = raw.get("name", "").strip()
            entity_type = (raw.get("entity_type") or "concept").strip().lower() or "concept"
            if not name:
                continue

            entity = GraphEntity(
                name=name,
                entity_type=entity_type,
                source_path=rel_path,
                properties=raw.get("properties", {}),
                confidence=ConfidenceLevel.INFERRED,
            )
            entities.append(entity)
            entity_name_map[name] = entity.id

        for raw in data.get("relationships", []):
            source_name = raw.get("source", "").strip()
            target_name = raw.get("target", "").strip()
            rel_type = raw.get("rel_type", "related_to")

            if not source_name or not target_name:
                continue

            # Validate type against ontology; track unknowns for evolution
            if not self._ontology.is_valid_relationship_type(rel_type):
                await self._track_unknown_rel_type(rel_type)
                rel_type = self._ontology.validate_relationship_type(rel_type)

            source_id = entity_name_map.get(source_name)
            target_id = entity_name_map.get(target_name)
            if not source_id or not target_id:
                logger.debug(
                    "llm_relationship_skipped",
                    path=rel_path,
                    source=source_name,
                    target=target_name,
                    source_found=bool(source_id),
                    target_found=bool(target_id),
                )
                continue

            # v2.2: Domain/range validation — reject structurally invalid edges
            if not self._check_domain_range(
                rel_type, source_name, target_name, entity_name_map, entities
            ):
                logger.debug(
                    "llm_relationship_domain_range_rejected",
                    rel_type=rel_type,
                    source=source_name,
                    target=target_name,
                )
                continue

            relationships.append(
                GraphRelationship(
                    source_id=source_id,
                    target_id=target_id,
                    rel_type=rel_type,
                    source_path=rel_path,
                    confidence=ConfidenceLevel.INFERRED,
                )
            )

        logger.info(
            "llm_extraction_complete",
            path=rel_path,
            entities=len(entities),
            relationships=len(relationships),
        )
        return entities, relationships

    def _check_domain_range(
        self,
        rel_type: str,
        source_name: str,
        target_name: str,
        entity_name_map: dict[str, str],
        entities: list[GraphEntity],
    ) -> bool:
        """Validate that source/target entity types match the relation's domain/range.

        Returns True if valid or if no constraints exist.
        """
        rel_info = self._ontology.get_relation_types().get(rel_type)
        if not rel_info:
            return True  # unknown type, let fallback handle it

        domain = rel_info.get("domain", "*")
        range_ = rel_info.get("range", "*")
        if domain == "*" and range_ == "*":
            return True

        # Build entity type lookup from current extraction
        type_lookup: dict[str, str] = {}
        for e in entities:
            type_lookup[e.name] = e.entity_type

        source_type = type_lookup.get(source_name, "concept")
        target_type = type_lookup.get(target_name, "concept")

        def _matches(constraint: str | list[str], actual: str) -> bool:
            if constraint == "*":
                return True
            if isinstance(constraint, list):
                return actual in constraint
            return actual == constraint

        if not _matches(domain, source_type):
            return False
        return _matches(range_, target_type)

    async def _track_unknown_rel_type(self, rel_type: str) -> None:
        """Track unknown relationship types and auto-evolve ontology when threshold is reached."""
        if not self._auto_evolve:
            return
        self._unknown_rel_type_counts[rel_type] = self._unknown_rel_type_counts.get(rel_type, 0) + 1
        if self._unknown_rel_type_counts[rel_type] >= self._unknown_threshold:
            added = await self._ontology.add_relationship_type(
                rel_type, f"Auto-discovered relationship: {rel_type}"
            )
            if added:
                logger.info("ontology_rel_type_auto_evolved", new_type=rel_type)
                del self._unknown_rel_type_counts[rel_type]
