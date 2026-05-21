"""VaultBackend — NetworkX-based graph backend for local/self-hosted BSage.

Markdown files are the source of truth. The graph is kept in memory as a
NetworkX MultiDiGraph and persisted to ``.bsage/graph_cache.json`` for fast
restarts. No external database is needed.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime
from itertools import combinations
from typing import Any

import networkx as nx
import structlog

from backend.knowledge.graph.graph_backend import GraphBackend
from backend.knowledge.graph.graph_models import (
    ConfidenceLevel,
    GraphEntity,
    GraphRelationship,
    Hyperedge,
    ProvenanceRecord,
    normalize_name,
)
from backend.knowledge.graph.storage import StorageBackend

logger = structlog.get_logger(__name__)

_CACHE_PATH = ".bsage/graph_cache.json"


def _entity_from_node(node_id: str, attrs: dict[str, Any]) -> GraphEntity:
    """Reconstruct a GraphEntity from NetworkX node attributes."""
    return GraphEntity(
        id=node_id,
        name=attrs.get("name", ""),
        entity_type=attrs.get("entity_type", ""),
        source_path=attrs.get("source_path", ""),
        properties=attrs.get("properties", {}),
        confidence=attrs.get("confidence", ConfidenceLevel.EXTRACTED),
        knowledge_layer=attrs.get("knowledge_layer"),
    )


def _rel_from_edge(u: str, v: str, key: str, attrs: dict[str, Any]) -> GraphRelationship:
    """Reconstruct a GraphRelationship from NetworkX edge attributes."""
    return GraphRelationship(
        id=key,
        source_id=u,
        target_id=v,
        rel_type=attrs.get("rel_type", ""),
        source_path=attrs.get("source_path", ""),
        properties=attrs.get("properties", {}),
        confidence=attrs.get("confidence", ConfidenceLevel.EXTRACTED),
        weight=attrs.get("weight", 0.5),
        edge_type=attrs.get("edge_type", "weak"),
        valid_from=attrs.get("valid_from"),
        valid_to=attrs.get("valid_to"),
        recorded_at=attrs.get("recorded_at"),
    )


class VaultBackend(GraphBackend):
    """NetworkX-based graph backend backed by markdown files."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage
        self._G: nx.MultiDiGraph = nx.MultiDiGraph()
        self._G.graph["hyperedges"] = {}
        self._source_hashes: dict[str, str] = {}
        self._provenance: dict[str, set[str]] = {}  # entity_id -> {source_paths}
        self._name_index: dict[tuple[str, str], str] = {}  # (norm_name, type) -> id
        self._lock = asyncio.Lock()

    # -- Lifecycle --------------------------------------------------------

    async def initialize(self) -> None:
        """Load graph from cache if available."""
        if await self._storage.exists(_CACHE_PATH):
            try:
                raw = await self._storage.read(_CACHE_PATH)
                data = json.loads(raw)
                self._G = nx.node_link_graph(
                    data["graph"], directed=True, multigraph=True, edges="links"
                )
                if "hyperedges" not in self._G.graph:
                    self._G.graph["hyperedges"] = {}
                self._source_hashes = data.get("source_hashes", {})
                self._provenance = {k: set(v) for k, v in data.get("provenance", {}).items()}
                self._rebuild_name_index()
                logger.info(
                    "cache_loaded",
                    entities=self._G.number_of_nodes(),
                    relationships=self._G.number_of_edges(),
                )
            except Exception:
                logger.warning("cache_load_failed", exc_info=True)
                self._G = nx.MultiDiGraph()
                self._G.graph["hyperedges"] = {}

    async def close(self) -> None:
        """Persist graph cache to storage."""
        await self._save_cache()

    async def _save_cache(self) -> None:
        data = {
            "graph": nx.node_link_data(self._G, edges="links"),
            "source_hashes": self._source_hashes,
            "provenance": {k: sorted(v) for k, v in self._provenance.items()},
        }
        await self._storage.write(_CACHE_PATH, json.dumps(data, ensure_ascii=False))

    def _rebuild_name_index(self) -> None:
        self._name_index.clear()
        for node_id, attrs in self._G.nodes(data=True):
            key = (normalize_name(attrs.get("name", "")), attrs.get("entity_type", ""))
            self._name_index[key] = node_id

    # -- Entity / Relationship CRUD ---------------------------------------

    async def upsert_entity(self, entity: GraphEntity) -> str:
        async with self._lock:
            key = (normalize_name(entity.name), entity.entity_type)
            existing_id = self._name_index.get(key)

            if existing_id:
                self._G.nodes[existing_id].update(
                    {
                        "name": entity.name,
                        "entity_type": entity.entity_type,
                        "source_path": entity.source_path,
                        "properties": entity.properties,
                        "confidence": entity.confidence,
                        "knowledge_layer": entity.knowledge_layer,
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                )
                return existing_id

            self._G.add_node(
                entity.id,
                name=entity.name,
                entity_type=entity.entity_type,
                source_path=entity.source_path,
                properties=entity.properties,
                confidence=entity.confidence,
                knowledge_layer=entity.knowledge_layer,
                created_at=datetime.now(UTC).isoformat(),
                updated_at=datetime.now(UTC).isoformat(),
            )
            self._name_index[key] = entity.id
            return entity.id

    async def upsert_relationship(self, rel: GraphRelationship) -> str:
        recorded = rel.recorded_at or datetime.now(UTC).isoformat()
        async with self._lock:
            # Check for existing edge with same (source, target, rel_type)
            if self._G.has_edge(rel.source_id, rel.target_id):
                for key, attrs in self._G[rel.source_id][rel.target_id].items():
                    if attrs.get("rel_type") == rel.rel_type:
                        self._G[rel.source_id][rel.target_id][key].update(
                            {
                                "source_path": rel.source_path,
                                "properties": rel.properties,
                                "confidence": rel.confidence,
                                "weight": rel.weight,
                                "edge_type": rel.edge_type,
                                "valid_from": rel.valid_from,
                                "valid_to": rel.valid_to,
                                "recorded_at": recorded,
                            }
                        )
                        return key

            self._G.add_edge(
                rel.source_id,
                rel.target_id,
                key=rel.id,
                rel_type=rel.rel_type,
                source_path=rel.source_path,
                properties=rel.properties,
                confidence=rel.confidence,
                weight=rel.weight,
                edge_type=rel.edge_type,
                valid_from=rel.valid_from,
                valid_to=rel.valid_to,
                recorded_at=recorded,
            )
            return rel.id

    async def delete_by_source(self, source_path: str) -> int:
        async with self._lock:
            deleted = 0
            nodes_to_remove = []

            for node_id, attrs in list(self._G.nodes(data=True)):
                if attrs.get("source_path") == source_path:
                    sources = self._provenance.get(node_id, set())
                    sources.discard(source_path)
                    if not sources:
                        nodes_to_remove.append(node_id)
                        self._provenance.pop(node_id, None)
                    else:
                        self._provenance[node_id] = sources

            for node_id in nodes_to_remove:
                attrs = self._G.nodes[node_id]
                key = (normalize_name(attrs.get("name", "")), attrs.get("entity_type", ""))
                self._name_index.pop(key, None)
                self._G.remove_node(node_id)
                deleted += 1

            # Remove edges from this source
            edges_to_remove = [
                (u, v, k)
                for u, v, k, d in self._G.edges(keys=True, data=True)
                if d.get("source_path") == source_path
            ]
            for u, v, k in edges_to_remove:
                self._G.remove_edge(u, v, key=k)
                deleted += 1

            return deleted

    # -- Queries ----------------------------------------------------------

    async def get_entity_by_name(
        self, name: str, entity_type: str | None = None
    ) -> GraphEntity | None:
        if entity_type:
            node_id = self._name_index.get((normalize_name(name), entity_type))
            if node_id and self._G.has_node(node_id):
                return _entity_from_node(node_id, self._G.nodes[node_id])
            return None

        norm = normalize_name(name)
        for (n, _t), node_id in self._name_index.items():
            if n == norm and self._G.has_node(node_id):
                return _entity_from_node(node_id, self._G.nodes[node_id])
        return None

    async def search_entities(self, query: str, *, limit: int = 20) -> list[GraphEntity]:
        norm_q = normalize_name(query)
        results = []
        for node_id, attrs in self._G.nodes(data=True):
            if norm_q in normalize_name(attrs.get("name", "")):
                results.append(_entity_from_node(node_id, attrs))
                if len(results) >= limit:
                    break
        return results

    async def query_neighbors(
        self, entity_id: str, *, rel_type: str | None = None
    ) -> list[tuple[GraphRelationship, GraphEntity]]:
        if not self._G.has_node(entity_id):
            return []

        results: list[tuple[GraphRelationship, GraphEntity]] = []

        # Outgoing edges
        for _, v, key, data in self._G.out_edges(entity_id, keys=True, data=True):
            if rel_type and data.get("rel_type") != rel_type:
                continue
            rel = _rel_from_edge(entity_id, v, key, data)
            ent = _entity_from_node(v, self._G.nodes[v])
            results.append((rel, ent))

        # Incoming edges
        for u, _, key, data in self._G.in_edges(entity_id, keys=True, data=True):
            if rel_type and data.get("rel_type") != rel_type:
                continue
            rel = _rel_from_edge(u, entity_id, key, data)
            ent = _entity_from_node(u, self._G.nodes[u])
            results.append((rel, ent))

        return results

    async def multi_hop_query(
        self, entity_id: str, *, max_hops: int = 2
    ) -> list[tuple[int, GraphEntity]]:
        if not self._G.has_node(entity_id):
            return []

        results: list[tuple[int, GraphEntity]] = []
        visited: set[str] = {entity_id}
        queue: deque[tuple[str, int]] = deque([(entity_id, 0)])

        while queue:
            current, depth = queue.popleft()
            if depth > 0:
                attrs = self._G.nodes[current]
                results.append((depth, _entity_from_node(current, attrs)))
            if depth < max_hops:
                neighbors = set(self._G.successors(current)) | set(self._G.predecessors(current))
                for n in neighbors:
                    if n not in visited:
                        visited.add(n)
                        queue.append((n, depth + 1))

        return results

    # -- Counts -----------------------------------------------------------

    async def count_entities(self) -> int:
        return self._G.number_of_nodes()

    async def count_relationships(self) -> int:
        return self._G.number_of_edges()

    async def count_entities_of_type(self, entity_type: str) -> int:
        return sum(1 for _, d in self._G.nodes(data=True) if d.get("entity_type") == entity_type)

    async def count_relationships_for_entity(self, entity_name: str) -> int:
        entity = await self.get_entity_by_name(entity_name)
        if not entity:
            return 0
        return self._G.degree(entity.id)

    async def count_distinct_sources(self, entity_name: str) -> int:
        entity = await self.get_entity_by_name(entity_name)
        if not entity:
            return 0
        return len(self._provenance.get(entity.id, set()))

    async def get_entity_updated_at(self, entity_name: str) -> str | None:
        entity = await self.get_entity_by_name(entity_name)
        if not entity:
            return None
        attrs = self._G.nodes.get(entity.id, {})
        return attrs.get("updated_at")

    # -- Source hashing ---------------------------------------------------

    async def get_source_hash(self, source_path: str) -> str | None:
        return self._source_hashes.get(source_path)

    async def set_source_hash(self, source_path: str, content_hash: str) -> None:
        self._source_hashes[source_path] = content_hash

    async def remove_source_hash(self, source_path: str) -> None:
        self._source_hashes.pop(source_path, None)

    # -- Provenance -------------------------------------------------------

    async def add_provenance(self, record: ProvenanceRecord) -> None:
        if record.entity_id not in self._provenance:
            self._provenance[record.entity_id] = set()
        self._provenance[record.entity_id].add(record.source_path)

    # -- Rebuild ----------------------------------------------------------

    async def rebuild_from_vault(
        self, storage: StorageBackend, extractor: object
    ) -> dict[str, int]:
        """Incremental rebuild from vault markdown files.

        Uses SHA256 content hashing to skip unchanged files.
        ``extractor`` must have an ``extract_from_note(rel_path, content)`` method.
        """
        from backend.knowledge.graph.graph_extractor import GraphExtractor

        if not isinstance(extractor, GraphExtractor):
            msg = "extractor must be a GraphExtractor instance"
            raise TypeError(msg)

        scan_dirs = [
            "seeds",
            "ideas",
            "insights",
            "projects",
            "people",
            "events",
            "tasks",
            "facts",
            "preferences",
            "garden",
        ]

        stats: dict[str, int] = {
            "entities_added": 0,
            "relationships_added": 0,
            "files_scanned": 0,
            "files_skipped": 0,
        }

        for subdir in scan_dirs:
            files = await storage.list_files(subdir)
            for rel_path in files:
                stats["files_scanned"] += 1

                try:
                    current_hash = await storage.content_hash(rel_path)
                except FileNotFoundError:
                    continue

                stored_hash = await self.get_source_hash(rel_path)
                if stored_hash == current_hash:
                    stats["files_skipped"] += 1
                    continue

                # Remove old data for this source
                await self.delete_by_source(rel_path)

                content = await storage.read(rel_path)
                entities, relationships = extractor.extract_from_note(rel_path, content)

                seen_ids: set[str] = set()
                for entity in entities:
                    eid = await self.upsert_entity(entity)
                    if eid not in seen_ids:
                        await self.add_provenance(
                            ProvenanceRecord(
                                entity_id=eid,
                                source_path=rel_path,
                                extraction_method="rule",
                                confidence=entity.confidence,
                                extracted_at=datetime.now(UTC).isoformat(),
                            )
                        )
                        seen_ids.add(eid)
                    stats["entities_added"] += 1

                for rel in relationships:
                    await self.upsert_relationship(rel)
                    stats["relationships_added"] += 1

                await self.set_source_hash(rel_path, current_hash)

        await self._save_cache()
        logger.info("rebuild_complete", **stats)
        return stats

    # -- NetworkX access --------------------------------------------------

    def to_networkx(self) -> nx.MultiDiGraph:
        """Return the internal graph directly."""
        return self._G

    # -- Hyperedge --------------------------------------------------------

    async def add_hyperedge(self, hyperedge: Hyperedge) -> str:
        async with self._lock:
            self._G.graph["hyperedges"][hyperedge.id] = {
                "id": hyperedge.id,
                "name": hyperedge.name,
                "relation": hyperedge.relation,
                "members": hyperedge.members,
                "source_path": hyperedge.source_path,
                "properties": hyperedge.properties,
                "confidence": hyperedge.confidence,
                "implicit_weight": hyperedge.implicit_weight,
            }

            # Create pairwise implicit edges between members
            valid_members = [m for m in hyperedge.members if self._G.has_node(m)]
            for a, b in combinations(valid_members, 2):
                self._G.add_edge(
                    a,
                    b,
                    key=f"he-{hyperedge.id}-{a}-{b}",
                    rel_type=hyperedge.relation,
                    source_path=hyperedge.source_path,
                    confidence=hyperedge.confidence,
                    weight=hyperedge.implicit_weight,
                    edge_type="weak",
                    via_hyperedge=hyperedge.id,
                    properties={},
                )

            return hyperedge.id

    async def get_hyperedges(self) -> list[Hyperedge]:
        return [Hyperedge(**data) for data in self._G.graph.get("hyperedges", {}).values()]
