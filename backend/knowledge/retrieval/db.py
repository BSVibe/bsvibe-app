"""Retrieval analytics schema — query log for ranking + budget telemetry."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class RetrievalBase(DeclarativeBase):
    """Declarative base for retrieval analytics tables."""


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
