"""Postgres + pgvector vector-search backend.

Uses pgvector's ``<=>`` (cosine distance) operator on
``intent_examples.embedding``. Requires the ``vector`` extension —
created in the alembic revision that adds these tables.

This backend is **prod-only** — it relies on the pgvector column type,
which SQLite doesn't have. Tests that exercise the real ``<=>`` path
must run against a live Postgres (a smoke test gated on
``BSVIBE_DATABASE_URL`` being reachable lives in
``tests/test_smoke.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.embedding.db import IntentExampleRow
from backend.embedding.storage.backend import (
    SearchHit,
    VectorEntry,
    VectorSearchBackend,
)


class PgVectorBackend(VectorSearchBackend):
    """Reads `intent_examples` directly; writes go through
    :class:`IntentRepository.add_example` /
    :class:`update_example_embedding` — this backend's :meth:`upsert`
    just forwards to the same session (UPSERT by id) so callers can use
    the same wire-shape as :class:`InMemoryVectorBackend`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, entries: Iterable[VectorEntry]) -> None:
        # PG INSERT ... ON CONFLICT (id) DO UPDATE — keep the embedding
        # surface uniform with the in-memory backend.
        for e in entries:
            await self._session.execute(
                text(
                    """
                    INSERT INTO intent_examples
                        (id, intent_id, workspace_id, account_id, text,
                         embedding, embedding_model, dimension, created_at)
                    VALUES
                        (:id, :intent_id, :ws, :acct, '',
                         :emb, :model, :dim, now())
                    ON CONFLICT (id) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        embedding_model = EXCLUDED.embedding_model,
                        dimension = EXCLUDED.dimension
                    """
                ),
                {
                    "id": e.id,
                    "intent_id": e.intent_id,
                    "ws": e.workspace_id,
                    "acct": e.account_id,
                    "emb": e.embedding,
                    "model": e.embedding_model,
                    "dim": len(e.embedding),
                },
            )
        await self._session.flush()

    async def remove_intent(self, intent_id: uuid.UUID) -> None:
        await self._session.execute(
            text("DELETE FROM intent_examples WHERE intent_id = :iid"),
            {"iid": intent_id},
        )
        await self._session.flush()

    async def search(
        self,
        *,
        query: list[float],
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        embedding_model: str,
        limit: int,
    ) -> list[SearchHit]:
        # cosine_distance = 1 - cosine_similarity ⇒ similarity = 1 - dist.
        rows = await self._session.execute(
            text(
                """
                SELECT id, intent_id, workspace_id, account_id,
                       embedding, embedding_model,
                       embedding <=> CAST(:qv AS vector) AS distance
                FROM intent_examples
                WHERE workspace_id = :ws
                  AND account_id = :acct
                  AND embedding_model = :model
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> CAST(:qv AS vector)
                LIMIT :lim
                """
            ),
            {
                "qv": query,
                "ws": workspace_id,
                "acct": account_id,
                "model": embedding_model,
                "lim": limit,
            },
        )
        hits: list[SearchHit] = []
        for r in rows.mappings():
            hits.append(
                SearchHit(
                    entry=VectorEntry(
                        id=r["id"],
                        workspace_id=r["workspace_id"],
                        account_id=r["account_id"],
                        intent_id=r["intent_id"],
                        embedding=list(r["embedding"]),
                        embedding_model=r["embedding_model"],
                    ),
                    similarity=1.0 - float(r["distance"]),
                )
            )
        return hits


# Re-export so callers don't reach into IntentExampleRow elsewhere.
__all__ = ["IntentExampleRow", "PgVectorBackend"]
