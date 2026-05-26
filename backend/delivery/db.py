"""Delivery persistence schema — delivery_events + safe_mode_queue_items.

Workflow §12.5 #8 (Bundle G — Delivery). Mirrors the canonicalization
db pattern (StrEnum + SAEnum named types, workspace_id NOT NULL +
indexed).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

DeliveryBase = Base


class SafeModeStatus(StrEnum):
    """Per Workflow §10.5 — queue item lifecycle."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    EXTENDED = "extended"


class DeliveryEventRow(DeliveryBase):
    """Persisted DeliveryResult row — one per dispatched deliverable.

    The ``deliverable_id`` column would naturally FK to the
    ``execution_runs.deliverables`` table from Bundle X, but the FK is
    intentionally left out at the SQLAlchemy layer — the two domains
    own separate :class:`DeclarativeBase` instances and we don't want
    cross-Base FK assertions to leak. The integrity is enforced via a
    raw FK at the migration / Alembic layer.

    ``run_id`` is nullable so legacy rows (pre-B12a) keep working — the
    DeliveryWorker threads it onto the SafeModeQueueItemRow it enqueues, so
    the founder can approve every queued item of a run as one transaction
    (Workflow §1.2 — Safe Mode as per-Run transactional container).
    """

    __tablename__ = "delivery_events"
    __table_args__ = (
        Index("ix_delivery_events_ws_created", "workspace_id", "created_at"),
        Index("ix_delivery_events_deliverable", "deliverable_id"),
        Index("ix_delivery_events_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    deliverable_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # B12a — the run this Deliver event came from. Nullable for back-compat
    # with rows seeded before the column existed.
    run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class SafeModeQueueItemRow(DeliveryBase):
    """Founder approval queue — Workflow §10.5 (90d + 2×30d retention).

    ``run_id`` is the per-Run grouping key (Workflow §1.2 — Safe Mode is the
    transactional container for a run's accumulated partial Deliver events).
    Nullable so existing rows survive the migration unchanged; new rows
    threaded by the DeliveryWorker always carry it.
    """

    __tablename__ = "safe_mode_queue_items"
    __table_args__ = (
        Index("ix_safe_mode_queue_ws_status", "workspace_id", "status"),
        Index("ix_safe_mode_queue_deliverable", "deliverable_id"),
        Index("ix_safe_mode_queue_ws_run", "workspace_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    deliverable_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # B12a — per-Run grouping key. Nullable for legacy rows; new rows always
    # set it via DeliveryWorker.
    run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    status: Mapped[SafeModeStatus] = mapped_column(
        SAEnum(
            SafeModeStatus,
            name="delivery_safe_mode_status_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
        default=SafeModeStatus.PENDING,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extension_count: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "DeliveryBase",
    "DeliveryEventRow",
    "SafeModeQueueItemRow",
    "SafeModeStatus",
]
