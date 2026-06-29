"""In-memory note vector backend — Python cosine, asyncio-safe (G3).

Used by the SQLite test suite and pure-Python dev mode. For prod / multi-process
see :class:`~backend.knowledge.retrieval.storage.pg.PgNoteVectorBackend`. One
instance per workspace vault, so it holds no workspace column (isolation is by
instance — mirrors the retired per-vault SQLite ``VectorStore``).
"""

from __future__ import annotations

import asyncio

from backend.knowledge.retrieval.storage.backend import cosine_similarity


class InMemoryNoteVectorBackend:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._embeddings: dict[str, list[float]] = {}

    async def store(self, note_path: str, embedding: list[float]) -> None:
        async with self._lock:
            self._embeddings[note_path] = list(embedding)

    async def remove(self, note_path: str) -> None:
        async with self._lock:
            self._embeddings.pop(note_path, None)

    async def existing_paths(self) -> set[str]:
        async with self._lock:
            return set(self._embeddings)

    async def search(
        self, query_embedding: list[float], top_k: int = 10
    ) -> list[tuple[str, float]]:
        async with self._lock:
            scored = [
                (path, cosine_similarity(query_embedding, emb))
                for path, emb in self._embeddings.items()
            ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
