"""In-memory vector backend — Python cosine, asyncio-safe.

Used by SQLite test conftest and pure-Python dev mode. For prod /
multi-process see :class:`PgVectorBackend`.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Iterable

from backend.router.embedding.storage.backend import (
    SearchHit,
    VectorEntry,
    VectorSearchBackend,
)


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class InMemoryVectorBackend(VectorSearchBackend):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: dict[uuid.UUID, VectorEntry] = {}

    async def upsert(self, entries: Iterable[VectorEntry]) -> None:
        async with self._lock:
            for e in entries:
                self._entries[e.id] = e

    async def remove_intent(self, intent_id: uuid.UUID) -> None:
        async with self._lock:
            doomed = [eid for eid, e in self._entries.items() if e.intent_id == intent_id]
            for eid in doomed:
                self._entries.pop(eid, None)

    async def search(
        self,
        *,
        query: list[float],
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        embedding_model: str,
        limit: int,
    ) -> list[SearchHit]:
        async with self._lock:
            candidates = [
                e
                for e in self._entries.values()
                if e.workspace_id == workspace_id
                and e.account_id == account_id
                and e.embedding_model == embedding_model
            ]
        hits = [SearchHit(entry=e, similarity=_cosine(query, e.embedding)) for e in candidates]
        hits.sort(key=lambda h: h.similarity, reverse=True)
        return hits[:limit]
