"""VectorStore — async SQLite-backed vector embedding storage with cosine similarity search."""

from __future__ import annotations

import math
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiosqlite
import structlog

from backend.knowledge.graph.write_queue import SQLiteWriteQueue

logger = structlog.get_logger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS embeddings (
    note_path TEXT PRIMARY KEY,
    embedding BLOB NOT NULL,
    dimension INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_emb_updated ON embeddings (updated_at);
"""


def _pack_embedding(embedding: list[float]) -> bytes:
    """Pack a float list into a compact binary blob."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _unpack_embedding(blob: bytes, dimension: int) -> list[float]:
    """Unpack a binary blob back into a float list."""
    return list(struct.unpack(f"{dimension}f", blob))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class VectorStore:
    """Async SQLite-backed vector storage for note embeddings.

    Stores dense vector embeddings as binary blobs and performs
    brute-force cosine similarity search. Suitable for personal
    vaults with up to ~10K notes.
    """

    def __init__(self, db_path: Path, *, write_queue_maxsize: int = 256) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._write_queue: SQLiteWriteQueue | None = None
        self._write_queue_maxsize = write_queue_maxsize

    async def initialize(self) -> None:
        """Open connection, create tables, start the write queue."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()
        self._write_queue = SQLiteWriteQueue(
            self._db,
            name="vector",
            maxsize=self._write_queue_maxsize,
        )
        await self._write_queue.start()
        logger.info("vector_store_initialized", path=str(self._db_path))

    async def close(self) -> None:
        """Drain the write queue and close the database connection."""
        if self._write_queue is not None:
            await self._write_queue.stop()
            self._write_queue = None
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "VectorStore not initialized — call initialize() first"
            raise RuntimeError(msg)
        return self._db

    async def _submit_write(self, op: Callable[[], Awaitable[None]]) -> None:
        if self._write_queue is None:
            msg = "VectorStore not initialized — call initialize() first"
            raise RuntimeError(msg)
        await self._write_queue.submit(op)

    async def store(self, note_path: str, embedding: list[float]) -> None:
        """Store or update an embedding for a note.

        Args:
            note_path: Vault-relative note path.
            embedding: Dense vector embedding.
        """
        blob = _pack_embedding(embedding)

        async def _op() -> None:
            await self._conn.execute(
                """INSERT OR REPLACE INTO embeddings (note_path, embedding, dimension, updated_at)
                   VALUES (?, ?, ?, datetime('now'))""",
                (note_path, blob, len(embedding)),
            )
            await self._conn.commit()

        await self._submit_write(_op)

    async def remove(self, note_path: str) -> None:
        """Remove an embedding for a note."""

        async def _op() -> None:
            await self._conn.execute("DELETE FROM embeddings WHERE note_path = ?", (note_path,))
            await self._conn.commit()

        await self._submit_write(_op)

    async def search(
        self, query_embedding: list[float], top_k: int = 10
    ) -> list[tuple[str, float]]:
        """Find the top-k most similar notes by cosine similarity.

        Args:
            query_embedding: Query vector.
            top_k: Number of results to return.

        Returns:
            List of (note_path, similarity_score) tuples, descending by score.
        """
        cursor = await self._conn.execute("SELECT note_path, embedding, dimension FROM embeddings")
        rows = await cursor.fetchall()

        scored: list[tuple[str, float]] = []
        for note_path, blob, dimension in rows:
            emb = _unpack_embedding(blob, dimension)
            score = _cosine_similarity(query_embedding, emb)
            scored.append((note_path, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    async def count(self) -> int:
        """Return the total number of stored embeddings."""
        cursor = await self._conn.execute("SELECT COUNT(*) FROM embeddings")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def has_embedding(self, note_path: str) -> bool:
        """Check if an embedding exists for a note."""
        cursor = await self._conn.execute(
            "SELECT 1 FROM embeddings WHERE note_path = ? LIMIT 1",
            (note_path,),
        )
        return await cursor.fetchone() is not None
