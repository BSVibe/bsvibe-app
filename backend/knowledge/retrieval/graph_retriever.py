"""GraphRetriever — graph-based note retrieval for knowledge graph RAG (v3.0).

v3.0 changes:
- Uses GraphBackend ABC instead of GraphStore directly
- Removed knowledge_layer decay (replaced by bi-temporal in Phase 2)
- Uses ConfidenceLevel enum for scoring
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from backend.knowledge.graph.graph_models import ConfidenceLevel

if TYPE_CHECKING:
    from backend.knowledge.graph.graph_backend import GraphBackend
    from backend.knowledge.graph.graph_models import GraphEntity
    from backend.knowledge.retrieval.ontology import OntologyRegistry
    from backend.knowledge.graph.vault import Vault

logger = structlog.get_logger(__name__)

# Numeric scores for confidence levels (used in retrieval ranking)
_CONFIDENCE_SCORES: dict[str, float] = {
    ConfidenceLevel.EXTRACTED: 1.0,
    ConfidenceLevel.INFERRED: 0.8,
    ConfidenceLevel.AMBIGUOUS: 0.4,
}


def _confidence_score(confidence: str) -> float:
    """Convert ConfidenceLevel to numeric score for ranking."""
    return _CONFIDENCE_SCORES.get(confidence, 0.5)


def _score(confidence: str, weight: float, depth: int) -> float:
    """Compute a retrieval relevance score.

    Higher is better.  ``confidence_score * weight / depth`` ensures that
    high-confidence, strong edges at shallow depth rank highest.
    """
    if depth <= 0:
        depth = 1
    return _confidence_score(confidence) * weight / depth


class GraphRetriever:
    """Retrieves vault notes by traversing the knowledge graph.

    Given a query, searches for matching entities in the graph,
    performs multi-hop BFS traversal, collects related note paths,
    and returns formatted context including relationship information.
    """

    def __init__(
        self,
        graph_store: GraphBackend,
        vault: Vault,
        ontology: OntologyRegistry | None = None,
    ) -> None:
        self._store = graph_store
        self._vault = vault
        self._ontology = ontology

    async def retrieve(
        self,
        query: str,
        *,
        max_hops: int = 2,
        top_k: int = 10,
        max_chars: int = 50_000,
    ) -> str:
        """Retrieve graph context for a query.

        Args:
            query: Search query — entity names are extracted and matched.
            max_hops: Maximum BFS traversal depth.
            top_k: Maximum number of related notes to include.
            max_chars: Maximum total characters in the result.

        Returns:
            Formatted string with graph relationships and note contents.
        """
        # 1. Find matching entities from query words
        matched = await self._match_entities(query)
        if not matched:
            return ""

        # 2. Multi-hop traversal from each matched entity
        # v2.2: track score = confidence * weight / depth for ranking
        source_scores: dict[str, float] = {}  # path -> best score
        graph_lines: list[str] = ["## Graph Context"]

        for entity in matched:
            neighbors = await self._store.query_neighbors(entity.id)
            graph_lines.append(f"\nEntity: **{entity.name}** ({entity.entity_type})")
            for rel, neighbor in neighbors:
                if rel.source_id == entity.id:
                    label = rel.rel_type
                    direction = "->"
                else:
                    # v2.2: use inverse relation name for incoming edges
                    label = self._resolve_inverse(rel.rel_type)
                    direction = "<-"
                graph_lines.append(
                    f"  {direction} {label} {direction} "
                    f"**{neighbor.name}** ({neighbor.entity_type})"
                )
                if neighbor.source_path:
                    s = _score(rel.confidence, rel.weight, 1)
                    source_scores[neighbor.source_path] = max(
                        source_scores.get(neighbor.source_path, 0.0), s
                    )

            # Deeper hops
            hops = await self._store.multi_hop_query(entity.id, max_hops=max_hops)
            for depth, hop_entity in hops:
                if hop_entity.source_path:
                    s = _score(hop_entity.confidence, 0.5, depth)
                    source_scores[hop_entity.source_path] = max(
                        source_scores.get(hop_entity.source_path, 0.0), s
                    )

            # Include the matched entity's own source (highest priority)
            if entity.source_path:
                source_scores[entity.source_path] = max(
                    source_scores.get(entity.source_path, 0.0), 10.0
                )

        # 3. Read note contents (sorted by score descending, limited to top_k)
        sorted_paths = sorted(source_scores.items(), key=lambda x: -x[1])[:top_k]

        parts: list[str] = ["\n".join(graph_lines)]
        total = len(parts[0])

        if sorted_paths:
            parts.append("\n## Related Notes")
            total += len(parts[-1])

        for path, _score_val in sorted_paths:
            if total >= max_chars:
                break
            try:
                abs_path = self._vault.resolve_path(path)
                content = await self._vault.read_note_content(abs_path)
                remaining = max_chars - total
                chunk = content[:remaining]
                parts.append(chunk)
                total += len(chunk)
            except (FileNotFoundError, OSError, UnicodeDecodeError):
                logger.debug("graph_retrieve_read_failed", path=path)

        logger.info(
            "graph_retrieve",
            query=query,
            matched=len(matched),
            notes=len(sorted_paths),
            total_chars=total,
        )
        return "\n---\n".join(parts)

    async def _match_entities(self, query: str, *, limit: int = 5) -> list[GraphEntity]:
        """Extract entity names from query and find matches in the graph."""
        # Strategy: try full query first, then individual words
        results: list[GraphEntity] = []
        seen_ids: set[str] = set()

        # Try full query as entity name
        full_matches = await self._store.search_entities(query, limit=limit)
        for e in full_matches:
            if e.id not in seen_ids:
                seen_ids.add(e.id)
                results.append(e)

        # Try individual words (skip short ones)
        words = [w.strip() for w in query.split() if len(w.strip()) >= 3]
        for word in words:
            if len(results) >= limit:
                break
            matches = await self._store.search_entities(word, limit=3)
            for e in matches:
                if e.id not in seen_ids:
                    seen_ids.add(e.id)
                    results.append(e)
                    if len(results) >= limit:
                        break

        return results[:limit]

    def _resolve_inverse(self, rel_type: str) -> str:
        """Return the inverse relation name, falling back to the original."""
        if self._ontology:
            inv = self._ontology.get_inverse(rel_type)
            if inv:
                return inv
        return rel_type
