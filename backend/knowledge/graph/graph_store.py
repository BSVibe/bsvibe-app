"""GraphStore — async SQLite-backed knowledge graph storage."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import aiosqlite
import networkx as nx
import structlog

from backend.knowledge.graph.graph_backend import GraphBackend
from backend.knowledge.graph.graph_models import (
    GraphEntity,
    GraphRelationship,
    Hyperedge,
    ProvenanceRecord,
    normalize_name,
)
from backend.knowledge.graph.write_queue import SQLiteWriteQueue

if TYPE_CHECKING:
    from backend.knowledge.graph.graph_extractor import GraphExtractor
    from backend.knowledge.graph.storage import StorageBackend
    from backend.knowledge.graph.vault import Vault

_T = TypeVar("_T")

logger = structlog.get_logger(__name__)

# ``knowledge_layer`` is a deprecated column (superseded by the bi-temporal
# model). The current schema declares it nullable, but databases created
# before that change still carry the original ``NOT NULL DEFAULT
# 'semantic'`` definition — and ``CREATE TABLE IF NOT EXISTS`` never
# rewrites an existing table. The extractor leaves the field ``None`` on
# most entity kinds, so an insert against an old database fails with
# ``NOT NULL constraint failed: entities.knowledge_layer``. Coalescing
# every write to the historical default keeps both schema generations
# happy without a risky SQLite table rebuild.
_DEFAULT_KNOWLEDGE_LAYER = "semantic"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_normalized TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    properties TEXT NOT NULL DEFAULT '{}',
    confidence TEXT NOT NULL DEFAULT 'extracted',
    knowledge_layer TEXT DEFAULT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_dedup
    ON entities (name_normalized, entity_type);

CREATE INDEX IF NOT EXISTS idx_entities_source
    ON entities (source_path);

CREATE INDEX IF NOT EXISTS idx_entities_type
    ON entities (entity_type);

CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    rel_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    properties TEXT NOT NULL DEFAULT '{}',
    confidence TEXT NOT NULL DEFAULT 'extracted',
    weight REAL NOT NULL DEFAULT 0.5,
    edge_type TEXT NOT NULL DEFAULT 'weak',
    valid_from TEXT DEFAULT NULL,
    valid_to TEXT DEFAULT NULL,
    recorded_at TEXT DEFAULT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_id) REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships (source_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships (target_id);
CREATE INDEX IF NOT EXISTS idx_rel_source_path ON relationships (source_path);
CREATE INDEX IF NOT EXISTS idx_rel_edge_type ON relationships (edge_type);

CREATE TABLE IF NOT EXISTS provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'extracted',
    extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_prov_dedup
    ON provenance (entity_id, source_path);

CREATE INDEX IF NOT EXISTS idx_prov_entity ON provenance (entity_id);
CREATE INDEX IF NOT EXISTS idx_prov_source ON provenance (source_path);

CREATE TABLE IF NOT EXISTS source_hashes (
    source_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _expected_schema() -> dict[str, list[dict[str, str]]]:
    """Per-table column list the current ``_SCHEMA_SQL`` declares.

    Derived by materialising the schema in a throwaway in-memory
    database, so it always tracks ``_SCHEMA_SQL`` with no hand-kept
    duplicate. Each column carries an ``ADD COLUMN``-ready ``ddl``
    fragment for :meth:`GraphStore._reconcile_schema`.
    """
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(_SCHEMA_SQL)
        tables = [
            row[0]
            for row in mem.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        ]
        schema: dict[str, list[dict[str, str]]] = {}
        for table in tables:
            cols: list[dict[str, str]] = []
            for _cid, name, ctype, notnull, dflt, _pk in mem.execute(
                f"PRAGMA table_info({table})"
            ).fetchall():
                ddl = f"{name} {ctype or 'TEXT'}"
                if notnull:
                    ddl += " NOT NULL"
                if dflt is not None:
                    ddl += f" DEFAULT {dflt}"
                cols.append({"name": name, "ddl": ddl})
            schema[table] = cols
        return schema
    finally:
        mem.close()


# Columns shared by both halves of the UNION ALL in query_neighbors.
_NEIGHBOR_COLS = (
    "r.id, r.source_id, r.target_id, r.rel_type,"
    " r.source_path, r.properties, r.confidence,"
    " r.weight, r.edge_type,"
    " e.id, e.name, e.name_normalized, e.entity_type,"
    " e.source_path, e.properties, e.confidence, e.knowledge_layer"
)


class GraphStore(GraphBackend):
    """Async SQLite-backed graph storage.

    Stores entities and relationships as a derived index over the vault.
    Uses WAL mode for concurrent read access.
    """

    def __init__(self, db_path: Path, *, write_queue_maxsize: int = 256) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._rebuild_lock: asyncio.Lock = asyncio.Lock()
        self._write_queue: SQLiteWriteQueue | None = None
        self._write_queue_maxsize = write_queue_maxsize
        self._nx_cache: nx.MultiDiGraph | None = None

    async def initialize(self) -> None:
        """Open connection, enable WAL mode, create tables, start writer."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()
        await self._reconcile_schema()
        # All writes funnel through this single-writer queue; reads stay
        # on the same aiosqlite connection but bypass the queue. See
        # bsage/garden/write_queue.py for the design rationale.
        self._write_queue = SQLiteWriteQueue(
            self._db,
            name="graph",
            maxsize=self._write_queue_maxsize,
        )
        await self._write_queue.start()
        logger.info("graph_store_initialized", path=str(self._db_path))

    async def _reconcile_schema(self) -> None:
        """Add columns the current schema declares but an older on-disk
        database is missing.

        ``executescript(_SCHEMA_SQL)`` uses ``CREATE TABLE IF NOT
        EXISTS``, which never alters an existing table — so a column
        added to the schema after a database was first created (e.g. the
        bi-temporal ``relationships.valid_from`` / ``valid_to`` set) is
        silently absent on older databases, and writes fail with
        ``table ... has no column named ...``. SQLite ``ALTER TABLE ADD
        COLUMN`` is additive and lossless. A column SQLite cannot add to
        a populated table (NOT NULL without a constant default, an
        expression default like ``datetime('now')``) is skipped with a
        warning rather than crashing startup.
        """
        assert self._db is not None
        for table, columns in _expected_schema().items():
            cursor = await self._db.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in await cursor.fetchall()}
            await cursor.close()
            if not existing:
                continue  # table absent — executescript already created it fresh
            for col in columns:
                if col["name"] in existing:
                    continue
                try:
                    await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col['ddl']}")
                    logger.info("graph_schema_column_added", table=table, column=col["name"])
                except (sqlite3.OperationalError, aiosqlite.Error) as exc:
                    logger.warning(
                        "graph_schema_column_unaddable",
                        table=table,
                        column=col["name"],
                        error=str(exc),
                    )
        await self._db.commit()

    async def close(self) -> None:
        """Drain the write queue and close the database connection."""
        if self._write_queue is not None:
            await self._write_queue.stop()
            self._write_queue = None
        if self._db:
            await self._db.close()
            self._db = None

    async def commit(self) -> None:
        """Commit the current transaction."""

        async def _commit() -> None:
            await self._conn.commit()

        await self._submit_write(_commit)

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "GraphStore not initialized — call initialize() first"
            raise RuntimeError(msg)
        return self._db

    async def _submit_write(self, op: Callable[[], Awaitable[_T]]) -> _T:
        """Submit ``op`` to the write queue, awaiting its result.

        Used as the single entry point for every mutating SQLite call,
        replacing the per-method ``async with _write_lock`` pattern.
        """
        if self._write_queue is None:
            msg = "GraphStore not initialized — call initialize() first"
            raise RuntimeError(msg)
        result: _T = await self._write_queue.submit(op)
        return result

    # ------------------------------------------------------------------
    # Entity CRUD
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: GraphEntity) -> str:
        """Insert or update an entity, returning its (possibly existing) ID.

        Deduplicates on ``(name_normalized, entity_type)``.  When a duplicate
        is found the existing row is updated.  A provenance record is always
        added so that ``delete_by_source`` can correctly determine whether
        the entity should be removed when one of its sources is deleted.
        """
        return await self._submit_write(lambda: self._upsert_entity_locked(entity))

    async def _upsert_entity_locked(self, entity: GraphEntity) -> str:
        # Caller must hold the write queue (i.e. is running on the writer task)
        norm = normalize_name(entity.name)
        row = await self._fetchone(
            "SELECT id FROM entities WHERE name_normalized = ? AND entity_type = ?",
            (norm, entity.entity_type),
        )
        if row:
            existing_id = str(row[0])
            await self._conn.execute(
                """UPDATE entities
                   SET source_path = ?, properties = ?, confidence = ?,
                       knowledge_layer = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (
                    entity.source_path,
                    json.dumps(entity.properties),
                    entity.confidence,
                    entity.knowledge_layer or _DEFAULT_KNOWLEDGE_LAYER,
                    existing_id,
                ),
            )
            # Record provenance for this source
            await self._conn.execute(
                """INSERT OR IGNORE INTO provenance
                   (entity_id, source_path, extraction_method, confidence, extracted_at)
                   VALUES (?, ?, 'rule', ?, datetime('now'))""",
                (existing_id, entity.source_path, entity.confidence),
            )
            return existing_id

        await self._conn.execute(
            """INSERT INTO entities (id, name, name_normalized, entity_type,
                                    source_path, properties, confidence, knowledge_layer)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entity.id,
                entity.name,
                norm,
                entity.entity_type,
                entity.source_path,
                json.dumps(entity.properties),
                entity.confidence,
                entity.knowledge_layer or _DEFAULT_KNOWLEDGE_LAYER,
            ),
        )
        # Record provenance for this source
        await self._conn.execute(
            """INSERT OR IGNORE INTO provenance
               (entity_id, source_path, extraction_method, confidence, extracted_at)
               VALUES (?, ?, 'rule', ?, datetime('now'))""",
            (entity.id, entity.source_path, entity.confidence),
        )
        return entity.id

    async def upsert_relationship(self, rel: GraphRelationship) -> str:
        """Insert a relationship, skipping duplicates on (source_id, target_id, rel_type)."""
        return await self._submit_write(lambda: self._upsert_relationship_locked(rel))

    async def _upsert_relationship_locked(self, rel: GraphRelationship) -> str:
        # Caller must be on the writer task
        row = await self._fetchone(
            """SELECT id FROM relationships
               WHERE source_id = ? AND target_id = ? AND rel_type = ?""",
            (rel.source_id, rel.target_id, rel.rel_type),
        )
        if row:
            return str(row[0])

        await self._conn.execute(
            """INSERT INTO relationships
               (id, source_id, target_id, rel_type, source_path, properties,
                confidence, weight, edge_type, valid_from, valid_to, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rel.id,
                rel.source_id,
                rel.target_id,
                rel.rel_type,
                rel.source_path,
                json.dumps(rel.properties),
                rel.confidence,
                rel.weight,
                rel.edge_type,
                rel.valid_from,
                rel.valid_to,
                rel.recorded_at,
            ),
        )
        return rel.id

    async def delete_by_source(self, source_path: str) -> int:
        """Remove entities and relationships originating from a source path.

        Uses provenance to determine which entities to delete:
        - Removes provenance records for this source_path.
        - Deletes entities that have no remaining provenance records
          (i.e., not referenced by any other source).
        - Relationships with source_path matching are always deleted.
        - FK cascade handles relationships referencing deleted entities.

        Returns the number of deleted entities.
        """
        return await self._submit_write(lambda: self._delete_by_source_locked(source_path))

    async def _delete_by_source_locked(self, source_path: str) -> int:
        # Caller must be on the writer task
        # 1. Delete relationships from this source
        await self._conn.execute("DELETE FROM relationships WHERE source_path = ?", (source_path,))

        # 2. Remove provenance records for this source
        await self._conn.execute("DELETE FROM provenance WHERE source_path = ?", (source_path,))

        # 3. Delete entities that have no remaining provenance AND
        #    whose current source_path matches (entities with other
        #    provenance records are kept).
        cursor = await self._conn.execute(
            """DELETE FROM entities
               WHERE source_path = ?
                 AND id NOT IN (SELECT entity_id FROM provenance)""",
            (source_path,),
        )

        # 4. Clean up source hash
        await self._conn.execute("DELETE FROM source_hashes WHERE source_path = ?", (source_path,))

        await self._conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Provenance
    # ------------------------------------------------------------------

    async def add_provenance(self, record: ProvenanceRecord) -> None:
        """Record extraction provenance for an entity.

        Skips duplicate (entity_id, source_path) combinations.
        """

        async def _op() -> None:
            await self._conn.execute(
                """INSERT OR IGNORE INTO provenance (entity_id, source_path, extraction_method,
                                                    confidence, extracted_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    record.entity_id,
                    record.source_path,
                    record.extraction_method,
                    record.confidence,
                    record.extracted_at,
                ),
            )
            await self._conn.commit()

        await self._submit_write(_op)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_entity_by_name(
        self, name: str, entity_type: str | None = None
    ) -> GraphEntity | None:
        """Look up an entity by (normalized) name and optional type."""
        norm = normalize_name(name)
        if entity_type:
            row = await self._fetchone(
                "SELECT * FROM entities WHERE name_normalized = ? AND entity_type = ?",
                (norm, entity_type),
            )
        else:
            row = await self._fetchone("SELECT * FROM entities WHERE name_normalized = ?", (norm,))
        return self._row_to_entity(row) if row else None

    async def search_entities(self, query: str, *, limit: int = 20) -> list[GraphEntity]:
        """Search entities by name substring (case-insensitive)."""
        norm = normalize_name(query)
        rows = await self._fetchall(
            """SELECT * FROM entities
               WHERE name_normalized LIKE '%' || ? || '%'
               ORDER BY name_normalized
               LIMIT ?""",
            (norm, limit),
        )
        return [self._row_to_entity(r) for r in rows]

    async def query_neighbors(
        self, entity_id: str, *, rel_type: str | None = None
    ) -> list[tuple[GraphRelationship, GraphEntity]]:
        """Find direct neighbors of an entity.

        Returns (relationship, neighbor_entity) pairs for both outgoing
        and incoming edges.
        """
        type_clause = " AND r.rel_type = ?" if rel_type else ""
        params: list[Any] = [entity_id]
        if rel_type:
            params.append(rel_type)
        params.append(entity_id)
        if rel_type:
            params.append(rel_type)

        sql = (
            "SELECT " + _NEIGHBOR_COLS + " FROM relationships r"
            " JOIN entities e ON e.id = r.target_id"
            " WHERE r.source_id = ?" + type_clause + " UNION ALL"
            " SELECT " + _NEIGHBOR_COLS + " FROM relationships r"
            " JOIN entities e ON e.id = r.source_id"
            " WHERE r.target_id = ?" + type_clause
        )
        rows = await self._fetchall(sql, tuple(params))
        results: list[tuple[GraphRelationship, GraphEntity]] = []
        for row in rows:
            try:
                rel_props = json.loads(row[5])
            except (json.JSONDecodeError, TypeError):
                logger.warning("corrupted_relationship_json", row_id=row[0])
                rel_props = {}
            try:
                ent_props = json.loads(row[14])
            except (json.JSONDecodeError, TypeError):
                logger.warning("corrupted_entity_json", row_id=row[9])
                ent_props = {}
            rel = GraphRelationship(
                source_id=row[1],
                target_id=row[2],
                rel_type=row[3],
                source_path=row[4],
                id=row[0],
                properties=rel_props,
                confidence=row[6],
                weight=row[7],
                edge_type=row[8],
            )
            ent = GraphEntity(
                name=row[10],
                entity_type=row[12],
                source_path=row[13],
                id=row[9],
                properties=ent_props,
                confidence=row[15],
                knowledge_layer=row[16],
            )
            results.append((rel, ent))
        return results

    async def multi_hop_query(
        self, entity_id: str, *, max_hops: int = 2
    ) -> list[tuple[int, GraphEntity]]:
        """BFS traversal from a starting entity up to ``max_hops`` depth.

        Returns ``(depth, entity)`` pairs, excluding the starting entity.
        Visited entities are never re-visited (cycle-safe).
        """
        visited: set[str] = {entity_id}
        frontier: list[str] = [entity_id]
        results: list[tuple[int, GraphEntity]] = []

        for depth in range(1, max_hops + 1):
            next_frontier: list[str] = []
            for eid in frontier:
                neighbors = await self.query_neighbors(eid)
                for _rel, neighbor in neighbors:
                    if neighbor.id not in visited:
                        visited.add(neighbor.id)
                        next_frontier.append(neighbor.id)
                        results.append((depth, neighbor))
            frontier = next_frontier
            if not frontier:
                break

        return results

    async def count_entities(self) -> int:
        """Return the total number of entities."""
        row = await self._fetchone("SELECT COUNT(*) FROM entities")
        return row[0] if row else 0

    async def count_relationships(self) -> int:
        """Return the total number of relationships."""
        row = await self._fetchone("SELECT COUNT(*) FROM relationships")
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Maturity-related queries
    # ------------------------------------------------------------------

    async def count_entities_of_type(self, entity_type: str) -> int:
        """Count entities with a given entity_type."""
        row = await self._fetchone(
            "SELECT COUNT(*) FROM entities WHERE entity_type = ?",
            (entity_type,),
        )
        return row[0] if row else 0

    async def count_relationships_for_entity(self, entity_name: str) -> int:
        """Count all relationships (inbound + outbound) for an entity.

        Matches by source_path or normalized entity name in a single query.
        """
        norm = normalize_name(entity_name)
        row = await self._fetchone(
            """SELECT COUNT(*) FROM (
                   SELECT r.id FROM relationships r
                   JOIN entities e ON e.id = r.source_id
                   WHERE e.source_path = ? OR e.name_normalized = ?
                   UNION
                   SELECT r.id FROM relationships r
                   JOIN entities e ON e.id = r.target_id
                   WHERE e.source_path = ? OR e.name_normalized = ?
               )""",
            (entity_name, norm, entity_name, norm),
        )
        return row[0] if row else 0

    async def count_distinct_sources(self, entity_name: str) -> int:
        """Count distinct source_path entries in provenance for an entity."""
        norm = normalize_name(entity_name)
        row = await self._fetchone(
            """SELECT COUNT(DISTINCT p.source_path) FROM provenance p
               JOIN entities e ON e.id = p.entity_id
               WHERE e.source_path = ?""",
            (entity_name,),
        )
        if row and row[0]:
            return int(row[0])
        row = await self._fetchone(
            """SELECT COUNT(DISTINCT p.source_path) FROM provenance p
               JOIN entities e ON e.id = p.entity_id
               WHERE e.name_normalized = ?""",
            (norm,),
        )
        return int(row[0]) if row else 0

    async def get_entity_updated_at(self, entity_name: str) -> str | None:
        """Return the updated_at timestamp for an entity."""
        norm = normalize_name(entity_name)
        row = await self._fetchone(
            "SELECT updated_at FROM entities WHERE source_path = ? LIMIT 1",
            (entity_name,),
        )
        if row:
            return str(row[0])
        row = await self._fetchone(
            "SELECT updated_at FROM entities WHERE name_normalized = ? LIMIT 1",
            (norm,),
        )
        return str(row[0]) if row else None

    # ------------------------------------------------------------------
    # Source of Truth — confirmation
    # ------------------------------------------------------------------

    async def confirm_entities_by_source(self, source_path: str) -> int:
        """Mark all entities from a source as freshly confirmed (updated_at = now).

        Called when a note is manually edited — the user's edits are
        the source of truth, so confidence resets its decay clock.

        Returns:
            Number of entities confirmed.
        """

        async def _op() -> int:
            cursor = await self._conn.execute(
                """UPDATE entities SET updated_at = datetime('now')
                   WHERE source_path = ?""",
                (source_path,),
            )
            await self._conn.commit()
            return cursor.rowcount

        return await self._submit_write(_op)

    # ------------------------------------------------------------------
    # Source content hashing (incremental rebuild)
    # ------------------------------------------------------------------

    async def get_source_hash(self, source_path: str) -> str | None:
        """Return the stored content hash for a source, or None."""
        row = await self._fetchone(
            "SELECT content_hash FROM source_hashes WHERE source_path = ?",
            (source_path,),
        )
        return row[0] if row else None

    async def set_source_hash(self, source_path: str, content_hash: str) -> None:
        """Store or update the content hash for a source."""

        async def _op() -> None:
            await self._set_source_hash_locked(source_path, content_hash)
            await self._conn.commit()

        await self._submit_write(_op)

    async def _set_source_hash_locked(self, source_path: str, content_hash: str) -> None:
        """Store or update content hash. Caller must be on the writer task."""
        await self._conn.execute(
            """INSERT OR REPLACE INTO source_hashes (source_path, content_hash, updated_at)
               VALUES (?, ?, datetime('now'))""",
            (source_path, content_hash),
        )

    async def remove_source_hash(self, source_path: str) -> None:
        """Remove the stored content hash for a source."""

        async def _op() -> None:
            await self._conn.execute(
                "DELETE FROM source_hashes WHERE source_path = ?", (source_path,)
            )
            await self._conn.commit()

        await self._submit_write(_op)

    # ------------------------------------------------------------------
    # Vault rebuild
    # ------------------------------------------------------------------

    async def rebuild_from_vault(
        self, storage: StorageBackend, extractor: object
    ) -> dict[str, int]:
        """Rebuild graph from vault notes, skipping unchanged content.

        Returns a dict with ``notes_updated``, ``notes_skipped``,
        ``entities``, and ``relationships`` counts.

        Uses ``_rebuild_lock`` to prevent concurrent rebuilds.
        """
        if TYPE_CHECKING:
            assert isinstance(storage, Vault)
            assert isinstance(extractor, GraphExtractor)
        async with self._rebuild_lock:
            return await self._rebuild_from_vault_locked(storage, extractor)

    async def _rebuild_from_vault_locked(
        self, vault: Vault, extractor: GraphExtractor
    ) -> dict[str, int]:
        # Caller must hold _rebuild_lock
        count = 0
        skipped = 0
        # v2.2: scan entity-type folders + seeds + legacy garden
        scan_dirs = (
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
        )
        for subdir in scan_dirs:
            base = vault.resolve_path(subdir)

            def _collect_md(p: Path = base) -> list[Path]:
                if not p.is_dir():
                    return []
                return sorted(p.rglob("*.md"))

            md_files = await asyncio.to_thread(_collect_md)
            for md_file in md_files:
                rel_path = str(md_file.relative_to(vault.root))
                try:
                    content = await vault.read_note_content(md_file)
                    content_hash = hashlib.sha256(content.encode()).hexdigest()

                    stored_hash = await self.get_source_hash(rel_path)
                    if stored_hash == content_hash:
                        skipped += 1
                        continue

                    async def _rebuild_one(
                        rp: str = rel_path,
                        ch: str = content_hash,
                        body: str = content,
                    ) -> None:
                        try:
                            await self._delete_by_source_locked(rp)
                            entities, rels = extractor.extract_from_note(rp, body)
                            id_map: dict[str, str] = {}
                            for entity in entities:
                                resolved_id = await self._upsert_entity_locked(entity)
                                id_map[entity.id] = resolved_id
                            for rel in rels:
                                resolved = dataclasses.replace(
                                    rel,
                                    source_id=id_map.get(rel.source_id, rel.source_id),
                                    target_id=id_map.get(rel.target_id, rel.target_id),
                                )
                                await self._upsert_relationship_locked(resolved)
                            await self._set_source_hash_locked(rp, ch)
                            await self._conn.commit()
                        except Exception:
                            await self._conn.rollback()
                            raise

                    await self._submit_write(_rebuild_one)
                    count += 1
                except (FileNotFoundError, OSError, UnicodeDecodeError):
                    logger.warning("graph_rebuild_note_failed", path=rel_path, exc_info=True)

        return {
            "notes_updated": count,
            "notes_skipped": skipped,
            "entities": await self.count_entities(),
            "relationships": await self.count_relationships(),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        """Execute a read query and return all result rows."""
        return await self._fetchall(sql, params)

    async def execute_batch(
        self, statements: list[tuple[str, tuple[Any, ...]]], *, commit: bool = True
    ) -> int:
        """Execute multiple write statements through the write queue.

        Returns total number of affected rows.
        """

        async def _op() -> int:
            total = 0
            for sql, params in statements:
                cursor = await self._conn.execute(sql, params)
                total += cursor.rowcount
            if commit and total:
                await self._conn.commit()
            return total

        return await self._submit_write(_op)

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        cursor = await self._conn.execute(sql, params)
        return list(await cursor.fetchall())

    @staticmethod
    def _row_to_entity(row: Any) -> GraphEntity:
        """Convert a raw SQLite row to a GraphEntity.

        Expected column order: id, name, name_normalized, entity_type,
        source_path, properties, confidence, knowledge_layer, created_at, updated_at.
        """
        try:
            props = json.loads(row[5])
        except (json.JSONDecodeError, TypeError):
            logger.warning("corrupted_entity_json", entity_id=row[0])
            props = {}
        return GraphEntity(
            name=row[1],
            entity_type=row[3],
            source_path=row[4],
            id=row[0],
            properties=props,
            confidence=row[6],
            knowledge_layer=row[7],
        )

    # ------------------------------------------------------------------
    # GraphBackend — NetworkX snapshot
    # ------------------------------------------------------------------

    def to_networkx(self) -> nx.MultiDiGraph:
        """Return the last built snapshot (may be stale).

        Callers that need fresh data should use ``to_networkx_snapshot()``
        (async, rebuilds from SQLite).
        """
        if self._nx_cache is None:
            self._nx_cache = nx.MultiDiGraph()
        return self._nx_cache

    async def to_networkx_snapshot(self) -> nx.MultiDiGraph:
        """Build a fresh NetworkX snapshot from all SQLite data."""
        graph = nx.MultiDiGraph()
        entities = await self._fetchall("SELECT * FROM entities")
        for row in entities:
            ent = self._row_to_entity(row)
            graph.add_node(
                ent.id,
                name=ent.name,
                entity_type=ent.entity_type,
                source_path=ent.source_path,
                confidence=ent.confidence,
                knowledge_layer=ent.knowledge_layer,
            )
        rels = await self._fetchall("SELECT * FROM relationships")
        for row in rels:
            try:
                props = json.loads(row[5])
            except (json.JSONDecodeError, TypeError):
                props = {}
            graph.add_edge(
                row[1],  # source_id
                row[2],  # target_id
                key=row[0],  # id
                rel_type=row[3],
                source_path=row[4],
                properties=props,
                confidence=row[6],
                weight=row[7],
                edge_type=row[8],
                valid_from=row[9] if len(row) > 9 else None,
                valid_to=row[10] if len(row) > 10 else None,
                recorded_at=row[11] if len(row) > 11 else None,
            )
        self._nx_cache = graph
        return graph

    # ------------------------------------------------------------------
    # GraphBackend — Hyperedge (stub for SQLite backend)
    # ------------------------------------------------------------------

    async def add_hyperedge(self, hyperedge: Hyperedge) -> str:
        """Add a hyperedge. Not yet implemented for SQLite backend."""
        raise NotImplementedError("Hyperedge support requires VaultBackend")

    async def get_hyperedges(self) -> list[Hyperedge]:
        """Get all hyperedges. Not yet implemented for SQLite backend."""
        raise NotImplementedError("Hyperedge support requires VaultBackend")
