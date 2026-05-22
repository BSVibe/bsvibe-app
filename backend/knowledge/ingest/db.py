"""Ingest analytics schema — per-batch summaries for telemetry + debugging."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

IngestBase = Base


class IngestBatch(IngestBase):
    """One row per ``IngestCompiler.compile_batch`` invocation."""

    __tablename__ = "ingest_batches"
    __table_args__ = (Index("ix_ingest_batches_ws_created", "workspace_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    seed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    decisions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
