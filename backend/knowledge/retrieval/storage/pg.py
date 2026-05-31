"""Postgres + pgvector note vector backend (G3).

Uses pgvector's ``<=>`` (cosine distance) on ``note_embeddings.embedding``,
scoped to one ``workspace_id``. Requires the ``vector`` extension (created in the
alembic revision that adds the table). **Prod-only** — it relies on the pgvector
column type, which SQLite lacks; the real ``<=>`` path is exercised by the
fresh-PG migration test + a ``BSVIBE_DATABASE_URL``-gated smoke test (mirrors
:class:`~backend.embedding.storage.pg.PgVectorBackend`).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _to_pgvector(embedding: list[float]) -> str:
    """Encode a float list as pgvector's text input (``[v1,v2,...]``).

    Raw ``text()`` SQL binds go straight to asyncpg, which has no codec for the
    pgvector ``vector`` type — passing a Python ``list`` raises ``DataError:
    expected str, got list``. pgvector accepts its text representation cast with
    ``CAST(... AS vector)``, so every embedding bind goes through this."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


class PgNoteVectorBackend:
    """Note vector store backed by ``note_embeddings``, scoped to one workspace.

    Carries the ``embedding_model`` so search only compares vectors from the same
    model (mixing models is meaningless) — the model rides onto every ``store``.
    """

    def __init__(
        self, session: AsyncSession, *, workspace_id: uuid.UUID, embedding_model: str
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._embedding_model = embedding_model

    async def store(self, note_path: str, embedding: list[float]) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO note_embeddings
                    (workspace_id, note_path, embedding, embedding_model, dimension, updated_at)
                VALUES
                    (:ws, :path, CAST(:emb AS vector), :model, :dim, now())
                ON CONFLICT (workspace_id, note_path) DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    embedding_model = EXCLUDED.embedding_model,
                    dimension = EXCLUDED.dimension,
                    updated_at = now()
                """
            ),
            {
                "ws": self._workspace_id,
                "path": note_path,
                "emb": _to_pgvector(embedding),
                "model": self._embedding_model,
                "dim": len(embedding),
            },
        )
        await self._session.flush()

    async def remove(self, note_path: str) -> None:
        await self._session.execute(
            text("DELETE FROM note_embeddings WHERE workspace_id = :ws AND note_path = :path"),
            {"ws": self._workspace_id, "path": note_path},
        )
        await self._session.flush()

    async def search(
        self, query_embedding: list[float], top_k: int = 10
    ) -> list[tuple[str, float]]:
        # cosine_distance = 1 - cosine_similarity ⇒ similarity = 1 - dist.
        rows = await self._session.execute(
            text(
                """
                SELECT note_path,
                       embedding <=> CAST(:qv AS vector) AS distance
                FROM note_embeddings
                WHERE workspace_id = :ws
                  AND embedding_model = :model
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> CAST(:qv AS vector)
                LIMIT :lim
                """
            ),
            {
                "qv": _to_pgvector(query_embedding),
                "ws": self._workspace_id,
                "model": self._embedding_model,
                "lim": top_k,
            },
        )
        return [(r["note_path"], 1.0 - float(r["distance"])) for r in rows.mappings()]
