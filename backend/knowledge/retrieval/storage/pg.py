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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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

    async def existing_paths(self) -> set[str]:
        # Filtered by the current model: vectors from a different model are
        # incomparable, so reconcile treats them as missing and re-embeds.
        rows = await self._session.execute(
            text(
                "SELECT note_path FROM note_embeddings "
                "WHERE workspace_id = :ws AND embedding_model = :model"
            ),
            {"ws": self._workspace_id, "model": self._embedding_model},
        )
        return {row[0] for row in rows}

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


class SessionScopedNoteVectorBackend:
    """:class:`PgNoteVectorBackend` that owns a SHORT session per operation.

    Same :class:`~backend.knowledge.retrieval.storage.backend.NoteVectorBackend`
    Protocol — so it drops straight into ``VaultRetriever(vector_store=…)`` and
    nothing downstream changes shape.

    Why it has to exist: ``PgNoteVectorBackend`` holds ONE live ``AsyncSession``,
    and :class:`~backend.knowledge.ingest.ingest_compiler.IngestCompiler` calls
    ``find_related`` from CONCURRENT chunk tasks (``parallelism``, default 3). One
    ``AsyncSession`` is not safe for concurrent use, so a shared-session backend
    handed to the compiler would turn a read into an interleaving bug. The two
    ingest runtimes already carry this exact reasoning for their LLM adapters
    (E18/E19: *"each must own its own session"*); the vault search is the same
    shape and gets the same treatment.

    A session per call is also the honest lifetime here: these are short reads,
    and holding a pooled connection across an ingest batch is the
    ``idle_in_transaction`` outage shape (#632/#686/#680).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        workspace_id: uuid.UUID,
        embedding_model: str,
    ) -> None:
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._embedding_model = embedding_model

    def _bind(self, session: AsyncSession) -> PgNoteVectorBackend:
        return PgNoteVectorBackend(
            session, workspace_id=self._workspace_id, embedding_model=self._embedding_model
        )

    async def store(self, note_path: str, embedding: list[float]) -> None:
        async with self._session_factory() as session:
            await self._bind(session).store(note_path, embedding)
            await session.commit()

    async def remove(self, note_path: str) -> None:
        async with self._session_factory() as session:
            await self._bind(session).remove(note_path)
            await session.commit()

    async def search(
        self, query_embedding: list[float], top_k: int = 10
    ) -> list[tuple[str, float]]:
        async with self._session_factory() as session:
            return await self._bind(session).search(query_embedding, top_k)

    async def existing_paths(self) -> set[str]:
        async with self._session_factory() as session:
            return await self._bind(session).existing_paths()
