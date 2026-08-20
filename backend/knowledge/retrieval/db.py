"""Retrieval analytics schema — query log for ranking + budget telemetry,
plus the pgvector-backed note-embedding store (G3)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base
from backend.embedding.column import EmbeddingVector

RetrievalBase = Base


class NoteEmbeddingRow(RetrievalBase):
    """One note's dense embedding for semantic search (G3).

    Mirrors the gateway's ``intent_examples`` embedding policy: the
    :class:`~backend.embedding.column.EmbeddingVector` column is a
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
