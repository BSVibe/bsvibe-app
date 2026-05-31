"""Retrieval analytics schema — query log for ranking + budget telemetry,
plus the pgvector-backed note-embedding store (G3)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base
from backend.router.embedding.column import EmbeddingVector

RetrievalBase = Base


class NoteEmbeddingRow(RetrievalBase):
    """One note's dense embedding for semantic search (G3).

    Mirrors the gateway's ``intent_examples`` embedding policy: the
    :class:`~backend.router.embedding.column.EmbeddingVector` column is a
    pgvector ``vector`` on Postgres (enabling the ``<=>`` cosine-distance
    operator) and a packed-float BLOB on SQLite (so the broad test suite needs
    no live Postgres). Scoped by ``workspace_id`` so the shared table is
    multi-tenant safe; ``note_path`` is the vault-relative path. PK is the
    ``(workspace_id, note_path)`` pair so a re-embed upserts in place.
    """

    __tablename__ = "note_embeddings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    note_path: Mapped[str] = mapped_column(Text, primary_key=True)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVector(), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class RetrievalQuery(RetrievalBase):
    """One row per retrieval API invocation (truncated query for privacy)."""

    __tablename__ = "retrieval_queries"
    __table_args__ = (Index("ix_retrieval_queries_ws_created", "workspace_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
