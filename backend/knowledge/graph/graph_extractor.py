"""GraphExtractor — rule-based entity and relationship extraction from vault notes (v2.2)."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import structlog

from backend.knowledge._internal.patterns import WIKILINK_RE
from backend.knowledge.graph.graph_models import ConfidenceLevel, GraphEntity, GraphRelationship
from backend.knowledge.graph.markdown_utils import body_after_frontmatter, extract_frontmatter

if TYPE_CHECKING:
    from backend.knowledge.ingest.llm_extractor import LLMExtractor
    from backend.knowledge.retrieval.ontology import OntologyRegistry

logger = structlog.get_logger(__name__)

# Frontmatter keys that are NOT relation types (common metadata fields)
_META_KEYS = frozenset(
    {
        "type",
        "status",
        "source",
        "captured_at",
        "confidence",
        "knowledge_layer",
        "last_confirmed",
        "aliases",
        "title",
        "tags",
        "related",
        "subject",
        "predicate",
        "object",
        "valid_from",
        "valid_to",
        "supersedes",
        "source_type",
        "domain",
        "context",
        "valid_at",
    }
)


def _slug_from_path(path: str) -> str:
    """Derive a human-readable name from a vault-relative path."""
    stem = PurePosixPath(path).stem
    return stem.replace("-", " ").replace("_", " ").strip()


def _extract_wikilink_names(items: list[Any]) -> list[tuple[str, str]]:
    """Extract wikilink target names from a list of strings.

    Returns list of (name, confidence) tuples. A trailing ``?`` after
    the wikilink (e.g. ``[[Alice]]?``) marks the target as AMBIGUOUS.
    """
    results: list[tuple[str, str]] = []
    for item in items:
        if isinstance(item, str):
            # Check for [[name]]? pattern (ambiguous marker)
            ambiguous = item.rstrip().endswith("?")
            found = WIKILINK_RE.findall(item)
            if found:
                conf = ConfidenceLevel.AMBIGUOUS if ambiguous else ConfidenceLevel.EXTRACTED
                results.extend((name, conf) for name in found)
            else:
                plain = item.rstrip("? ") if ambiguous else item
                if plain:
                    conf = ConfidenceLevel.AMBIGUOUS if ambiguous else ConfidenceLevel.EXTRACTED
                    results.append((plain, conf))
    return results


class GraphExtractor:
    """Extracts entities and relationships from vault note content.

    Primary extraction is deterministic rule-based (confidence=EXTRACTED).
    When an ``LLMExtractor`` is provided, also extracts from unstructured
    body text using LLM (confidence=INFERRED).

    v2.2: Uses frontmatter type directly as entity_type (no _TYPE_MAP).
    Extracts typed relations from frontmatter keys matching ontology relation types.
    Distinguishes strong edges (frontmatter) from weak edges (body mentions).
    """

    def __init__(
        self,
        llm_extractor: LLMExtractor | None = None,
        ontology: OntologyRegistry | None = None,
    ) -> None:
        self._llm_extractor = llm_extractor
        self._ontology = ontology

    def extract_from_note(
        self, rel_path: str, content: str
    ) -> tuple[list[GraphEntity], list[GraphRelationship]]:
        """Extract entities and relationships from a single note.

        Args:
            rel_path: Vault-relative path (e.g. ``ideas/bsage.md``).
            content: Full markdown content of the note.

        Returns:
            Tuple of (entities, relationships).
        """
        entities: list[GraphEntity] = []
        relationships: list[GraphRelationship] = []

        fm = extract_frontmatter(content)
        body = body_after_frontmatter(content)

        # Determine knowledge layer from frontmatter, defaulting to semantic.
        # The static type→layer table went away with the entity_types enum;
        # callers that care about episodic / procedural now stamp the
        # frontmatter explicitly.
        fm_type = fm.get("type", "concept")
        knowledge_layer = fm.get("knowledge_layer", "semantic")

        # Bi-temporal: extract valid_from/valid_to from frontmatter
        note_valid_from = str(fm["valid_from"]) if fm.get("valid_from") else None
        note_valid_to = str(fm["valid_to"]) if fm.get("valid_to") else None
        if note_valid_to == "present":
            note_valid_to = None  # "present" means still valid

        # 1. Note entity — use frontmatter type directly as entity_type (v2.2: no mapping)
        note_name = fm.get("title") or _slug_from_path(rel_path)
        note_entity = GraphEntity(
            name=note_name,
            entity_type=fm_type,
            source_path=rel_path,
            properties={k: v for k, v in fm.items() if k in ("type", "status", "captured_at")},
            confidence=fm.get("confidence", ConfidenceLevel.EXTRACTED),
            knowledge_layer=knowledge_layer,
        )
        entities.append(note_entity)

        # Track all names captured via frontmatter to avoid body-link duplication
        all_related_names: set[str] = set()

        # 1b. Fact triple extraction (subject/predicate/object → typed edges)
        if fm_type == "fact":
            subject_raw = fm.get("subject", "")
            predicate = fm.get("predicate", "")
            object_raw = fm.get("object", "")
            subject_pairs = _extract_wikilink_names([subject_raw]) if subject_raw else []
            object_pairs = _extract_wikilink_names([object_raw]) if object_raw else []
            if subject_pairs and predicate and object_pairs:
                subj_name, subj_conf = subject_pairs[0]
                obj_name, obj_conf = object_pairs[0]
                subj_entity = GraphEntity(
                    name=subj_name,
                    entity_type="concept",
                    source_path=rel_path,
                    confidence=subj_conf,
                )
                obj_entity = GraphEntity(
                    name=obj_name,
                    entity_type="concept",
                    source_path=rel_path,
                    confidence=obj_conf,
                )
                entities.extend([subj_entity, obj_entity])
                relationships.append(
                    GraphRelationship(
                        source_id=subj_entity.id,
                        target_id=obj_entity.id,
                        rel_type=predicate,
                        source_path=rel_path,
                        weight=self._get_relation_weight(predicate),
                        edge_type="strong",
                        valid_from=note_valid_from,
                        valid_to=note_valid_to,
                    )
                )
                # Link fact note to subject
                relationships.append(
                    GraphRelationship(
                        source_id=note_entity.id,
                        target_id=subj_entity.id,
                        rel_type="related_to",
                        source_path=rel_path,
                        weight=0.8,
                        edge_type="strong",
                    )
                )
                all_related_names.update(n for n, _ in subject_pairs + object_pairs)
            # Supersedes chain
            supersedes_raw = fm.get("supersedes", "")
            if supersedes_raw:
                sup_pairs = _extract_wikilink_names([supersedes_raw])
                for sup_name, sup_conf in sup_pairs:
                    sup_entity = GraphEntity(
                        name=sup_name,
                        entity_type="fact",
                        source_path=rel_path,
                        confidence=sup_conf,
                    )
                    entities.append(sup_entity)
                    relationships.append(
                        GraphRelationship(
                            source_id=note_entity.id,
                            target_id=sup_entity.id,
                            rel_type="supersedes",
                            source_path=rel_path,
                            weight=self._get_relation_weight("supersedes"),
                            edge_type="strong",
                        )
                    )
                    all_related_names.add(sup_name)

        # 2. Tags → tag entities + tagged_with relationships (strong edges)
        tags = fm.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        for tag in tags:
            tag_str = str(tag).strip()
            if not tag_str:
                continue
            tag_entity = GraphEntity(name=tag_str, entity_type="tag", source_path=rel_path)
            entities.append(tag_entity)
            relationships.append(
                GraphRelationship(
                    source_id=note_entity.id,
                    target_id=tag_entity.id,
                    rel_type="tagged_with",
                    source_path=rel_path,
                    weight=self._get_relation_weight("tagged_with"),
                    edge_type="strong",
                )
            )

        # 3. Source → source entity + created_by relationship (strong edge)
        source = fm.get("source")
        if source:
            source_entity = GraphEntity(
                name=str(source), entity_type="source", source_path=rel_path
            )
            entities.append(source_entity)
            relationships.append(
                GraphRelationship(
                    source_id=note_entity.id,
                    target_id=source_entity.id,
                    rel_type="created_by",
                    source_path=rel_path,
                    weight=self._get_relation_weight("created_by"),
                    edge_type="strong",
                )
            )

        # 4. Typed relations from frontmatter keys (v2.2: key = relation type)
        relation_types = self._get_known_relation_types()

        for key, value in fm.items():
            if key in _META_KEYS or key not in relation_types:
                continue
            # Value must be a list of wikilinks
            if not isinstance(value, list):
                value = [value]
            target_pairs = _extract_wikilink_names(value)
            for target_name, target_conf in target_pairs:
                all_related_names.add(target_name)
                target_entity = GraphEntity(
                    name=target_name,
                    entity_type="concept",
                    source_path=rel_path,
                    confidence=target_conf,
                )
                entities.append(target_entity)
                relationships.append(
                    GraphRelationship(
                        source_id=note_entity.id,
                        target_id=target_entity.id,
                        rel_type=key,
                        source_path=rel_path,
                        weight=self._get_relation_weight(key),
                        edge_type="strong",
                        confidence=target_conf,
                    )
                )

        # 5. Untyped related wikilinks → related_to relationships (strong, weight 0.5)
        related = fm.get("related", [])
        if not isinstance(related, list):
            related = []
        related_pairs = _extract_wikilink_names(related)
        for target_name, target_conf in related_pairs:
            if target_name in all_related_names:
                continue
            all_related_names.add(target_name)
            target_entity = GraphEntity(
                name=target_name,
                entity_type="concept",
                source_path=rel_path,
                confidence=target_conf,
            )
            entities.append(target_entity)
            relationships.append(
                GraphRelationship(
                    source_id=note_entity.id,
                    target_id=target_entity.id,
                    rel_type="related_to",
                    source_path=rel_path,
                    weight=self._get_relation_weight("related_to"),
                    edge_type="strong",
                    confidence=target_conf,
                )
            )

        # 6. Body wikilinks → references relationships (weak edges, weight 0.1)
        body_links = WIKILINK_RE.findall(body)
        seen: set[str] = set()
        for link_name in body_links:
            if link_name in all_related_names or link_name in seen:
                continue
            seen.add(link_name)
            link_entity = GraphEntity(name=link_name, entity_type="concept", source_path=rel_path)
            entities.append(link_entity)
            relationships.append(
                GraphRelationship(
                    source_id=note_entity.id,
                    target_id=link_entity.id,
                    rel_type="references",
                    source_path=rel_path,
                    weight=0.1,
                    edge_type="weak",
                )
            )

        logger.debug(
            "graph_extracted",
            path=rel_path,
            entities=len(entities),
            relationships=len(relationships),
        )
        return entities, relationships

    async def extract_with_llm(
        self, rel_path: str, content: str
    ) -> tuple[list[GraphEntity], list[GraphRelationship]]:
        """Extract using rules first, then enhance with LLM if available.

        Returns combined results from both rule-based and LLM extraction.
        """
        entities, relationships = self.extract_from_note(rel_path, content)

        if self._llm_extractor is not None:
            body = body_after_frontmatter(content)
            llm_entities, llm_rels = await self._llm_extractor.extract(rel_path, body)
            entities.extend(llm_entities)
            relationships.extend(llm_rels)

        return entities, relationships

    def _get_known_relation_types(self) -> set[str]:
        """Return known relation type names from ontology."""
        if self._ontology is None:
            return set()
        return set(self._ontology.get_relation_types().keys())

    def _get_relation_weight(self, rel_type: str) -> float:
        """Get the default weight for a relation type from ontology."""
        if self._ontology is None:
            return 0.5
        return self._ontology.get_relation_weight(rel_type)
