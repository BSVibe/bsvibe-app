"""Vector-search backend abstraction.

Production uses :class:`PgVectorBackend` (SQL ``<=>``). Tests + in-memory
dev use :class:`InMemoryVectorBackend` (Python cosine). Same Protocol —
swap via DI.

Keeps the rules/intent layer DB-dialect-agnostic and lets us run the
full Bundle 1 SQLite test conftest without a live Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class VectorEntry:
    """One stored embedding, scoped to ``(workspace_id, account_id)``."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    intent_id: uuid.UUID
    embedding: list[float]
    embedding_model: str


@dataclass(frozen=True)
class SearchHit:
    entry: VectorEntry
    similarity: float


@runtime_checkable
class VectorSearchBackend(Protocol):
    async def upsert(self, entries: Iterable[VectorEntry]) -> None: ...

    async def remove_intent(self, intent_id: uuid.UUID) -> None: ...

    async def search(
        self,
        *,
        query: list[float],
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        embedding_model: str,
        limit: int,
    ) -> list[SearchHit]: ...
