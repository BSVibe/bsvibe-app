"""Ontology-correction persistence schema (Lift M3a).

One table — ``ontology_corrections`` — that carries every founder-issued
:class:`~backend.knowledge.domain.retraction.RetractionSignal` plus its
30-second undo-window lifecycle. The row IS the timer: ``apply_at`` is the
wall-clock deadline a sweep / lazy resolver gates the actual vault
tombstone write on, and ``applied_at`` / ``cancelled_at`` are the terminal
flags. A process restart between intake and apply does not lose the timer.

Schema mirrors the migration at
``backend/data/migrations/versions/20260621_ontology_corrections.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base


class OntologyCorrection(Base):
    """Durable record of one founder-issued retraction / correction.

    Idempotency: a second insert with the same ``id`` short-circuits to
    "already issued" at the application layer (``RetractionService.issue``).
    Workspace + node composite index supports the "is this node currently
    retracted?" query the retriever-side guard uses.

    Terminal states (mutually exclusive):

    * ``applied_at IS NULL AND cancelled_at IS NULL`` — in flight; pending
      apply / cancel.
    * ``applied_at IS NOT NULL`` — tombstone written to the vault.
    * ``cancelled_at IS NOT NULL`` — founder undid inside the window.
    """

    __tablename__ = "ontology_corrections"
    __table_args__ = (
        # ``ix_ontology_corrections_workspace_id`` is created column-level via
        # ``index=True`` on ``workspace_id`` below — SQLAlchemy auto-names it
        # ``ix_<table>_<col>`` which matches the migration. Defining a second
        # explicit ``Index(...)`` here would duplicate it and CREATE INDEX twice
        # on ``Base.metadata.create_all`` (e.g. the test SQLite engine).
        Index("ix_ontology_corrections_node_ref", "workspace_id", "node_ref"),
        # Sweep / lazy-resolve query — pending rows ordered by deadline.
        Index("ix_ontology_corrections_pending", "apply_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    node_ref: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    apply_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["OntologyCorrection"]
