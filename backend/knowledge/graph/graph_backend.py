"""GraphBackend — abstract interface for knowledge graph storage.

All graph backends (VaultBackend, GraphStore, future PGBackend) implement
this ABC. The ``to_networkx()`` method provides a NetworkX graph for
analysis algorithms (community detection, centrality, etc.).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import networkx as nx

if TYPE_CHECKING:
    from backend.knowledge.graph.graph_models import (
        GraphEntity,
        GraphRelationship,
        Hyperedge,
        ProvenanceRecord,
    )
    from backend.knowledge.graph.storage import StorageBackend


class GraphBackend(ABC):
    """Abstract knowledge graph storage backend."""

    # -- Lifecycle --------------------------------------------------------

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the backend (create tables, load cache, etc.)."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources and persist state."""

    # -- Entity / Relationship CRUD ---------------------------------------

    @abstractmethod
    async def upsert_entity(self, entity: GraphEntity) -> str:
        """Insert or update an entity. Returns the entity ID."""

    @abstractmethod
    async def upsert_relationship(self, rel: GraphRelationship) -> str:
        """Insert or update a relationship. Returns the relationship ID."""

    @abstractmethod
    async def delete_by_source(self, source_path: str) -> int:
        """Delete entities/relationships from a source, respecting provenance."""

    # -- Queries ----------------------------------------------------------

    @abstractmethod
    async def get_entity_by_name(
        self, name: str, entity_type: str | None = None
    ) -> GraphEntity | None:
        """Look up an entity by name (and optionally type)."""

    @abstractmethod
    async def search_entities(self, query: str, *, limit: int = 20) -> list[GraphEntity]:
        """Substring search over entity names."""

    @abstractmethod
    async def query_neighbors(
        self, entity_id: str, *, rel_type: str | None = None
    ) -> list[tuple[GraphRelationship, GraphEntity]]:
        """Return (relationship, neighbor) pairs for an entity."""

    @abstractmethod
    async def multi_hop_query(
        self, entity_id: str, *, max_hops: int = 2
    ) -> list[tuple[int, GraphEntity]]:
        """BFS traversal returning (depth, entity) tuples."""

    # -- Counts (for MaturityEvaluator / stats) ---------------------------

    @abstractmethod
    async def count_entities(self) -> int: ...

    @abstractmethod
    async def count_relationships(self) -> int: ...

    @abstractmethod
    async def count_entities_of_type(self, entity_type: str) -> int: ...

    @abstractmethod
    async def count_relationships_for_entity(self, entity_name: str) -> int: ...

    @abstractmethod
    async def count_distinct_sources(self, entity_name: str) -> int: ...

    @abstractmethod
    async def get_entity_updated_at(self, entity_name: str) -> str | None: ...

    # -- Source hashing (incremental rebuild) -----------------------------

    @abstractmethod
    async def get_source_hash(self, source_path: str) -> str | None: ...

    @abstractmethod
    async def set_source_hash(self, source_path: str, content_hash: str) -> None: ...

    @abstractmethod
    async def remove_source_hash(self, source_path: str) -> None: ...

    # -- Provenance -------------------------------------------------------

    @abstractmethod
    async def add_provenance(self, record: ProvenanceRecord) -> None: ...

    # -- Rebuild ----------------------------------------------------------

    @abstractmethod
    async def rebuild_from_vault(
        self, storage: StorageBackend, extractor: object
    ) -> dict[str, int]:
        """Full or incremental rebuild from vault markdown files."""

    # -- NetworkX access --------------------------------------------------

    @abstractmethod
    def to_networkx(self) -> nx.MultiDiGraph:
        """Return the graph as a NetworkX MultiDiGraph for analysis."""

    async def to_networkx_snapshot(self) -> nx.MultiDiGraph:
        """Return a fresh NetworkX snapshot, awaitable for IO-backed backends.

        Default implementation delegates to the synchronous ``to_networkx()``
        for in-memory backends (VaultBackend). Persistent backends
        (GraphStore) override this to rebuild from storage.
        """
        return self.to_networkx()

    # -- Temporal queries -------------------------------------------------

    async def query_valid_at(
        self, entity_id: str, at_date: str, *, rel_type: str | None = None
    ) -> list[tuple[GraphRelationship, GraphEntity]]:
        """Return relationships valid at a specific date.

        Filters neighbors by ``valid_from <= at_date`` and
        ``valid_to is None or valid_to > at_date``.
        Default implementation filters ``query_neighbors`` results.
        """
        neighbors = await self.query_neighbors(entity_id, rel_type=rel_type)
        results = []
        for rel, ent in neighbors:
            if rel.valid_from and rel.valid_from > at_date:
                continue
            if rel.valid_to and rel.valid_to <= at_date:
                continue
            results.append((rel, ent))
        return results

    async def invalidate_relationship(self, rel_id: str, invalid_at: str) -> bool:
        """Mark a relationship as no longer valid by setting valid_to.

        Returns True if the relationship was found and updated.
        Default implementation searches edges by key.
        """
        graph = self.to_networkx()
        for _u, _v, key, data in graph.edges(keys=True, data=True):
            if key == rel_id:
                data["valid_to"] = invalid_at
                return True
        return False

    # -- Hyperedge --------------------------------------------------------

    @abstractmethod
    async def add_hyperedge(self, hyperedge: Hyperedge) -> str:
        """Add an n-ary relationship. Returns the hyperedge ID."""

    @abstractmethod
    async def get_hyperedges(self) -> list[Hyperedge]: ...
