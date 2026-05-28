"""Note vector-search backend abstraction (G3).

Mirrors the gateway embedding policy (:mod:`backend.gateway.embedding.storage`):
production uses :class:`~backend.knowledge.retrieval.storage.pg.PgNoteVectorBackend`
(pgvector ``<=>``); tests + in-memory dev use
:class:`~backend.knowledge.retrieval.storage.memory.InMemoryNoteVectorBackend`
(Python cosine). Same Protocol — swap via DI.

The method signatures match the retired SQLite ``VectorStore`` exactly
(``store`` / ``remove`` / ``search``) so the existing consumers
(:class:`~backend.knowledge.retrieval.vector_subscriber.VectorSubscriber`,
:class:`~backend.knowledge.retrieval.retriever.VaultRetriever`) depend only on
this Protocol. Workspace scoping is per-backend-instance (one per vault), so the
note-facing methods carry no workspace argument.
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 on a zero vector or a
    length mismatch (never raises — a malformed embedding must not break search)."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@runtime_checkable
class NoteVectorBackend(Protocol):
    """A workspace-scoped note embedding store with similarity search."""

    async def store(self, note_path: str, embedding: list[float]) -> None: ...

    async def remove(self, note_path: str) -> None: ...

    async def search(
        self, query_embedding: list[float], top_k: int = 10
    ) -> list[tuple[str, float]]: ...
