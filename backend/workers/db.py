"""Workers persistence schema — the settle-drain high-water marks.

Workflow §12.5 #8 (Bundle G — Workers) originally declared four tables here:
worker registration, install-token issuance, the audit-relay cursor, and the
settle drains. **Only the last one was ever written.** The other three were
dropped 2026-08-21 with prod row counts of 0 — worker registration is owned by
:class:`backend.executors.db.WorkerRow` (``executor_workers``, live), and
``executors/db.py`` had already declared the install-token ORM "gone".
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

WorkersBase = Base


class SettleDrainRow(WorkersBase):
    """One row per ``settle`` activity already absorbed into BSage.

    The :class:`~backend.knowledge.infrastructure.workers.settle_worker.SettleWorker` (the §4
    ``worker-settle`` BSage write subscriber) inserts a row here after it
    writes a settle observation into a workspace's vault. The activity id
    is the primary key, so a re-drain that re-selects the same activity is
    a no-op — this is the table that makes the drain idempotent. It is not
    a deletable queue (unlike ``delivery_events``): the source
    ``execution_run_activities`` rows are append-only telemetry the trace
    UI reads, so we mark drained out-of-band instead of consuming them.
    """

    __tablename__ = "settle_drains"
    __table_args__ = (Index("ix_settle_drains_workspace_id", "workspace_id"),)

    activity_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # Vault path of the written note, or NULL when the sink wrote nothing.
    node_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    drained_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


__all__ = [
    "SettleDrainRow",
    "WorkersBase",
]
