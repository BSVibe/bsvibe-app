"""Workers persistence schema — workers + install_tokens + audit_relay_state.

Workflow §12.5 #8 (Bundle G — Workers). Tracks worker registration,
install-token issuance, and the audit-relay cursor.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class WorkersBase(DeclarativeBase):
    """Declarative base for workers-domain tables."""


class WorkerStatus(StrEnum):
    """Worker liveness — driven by heartbeat freshness."""

    IDLE = "idle"
    RUNNING = "running"
    FAILED = "failed"
    DEAD = "dead"


class WorkerRow(WorkersBase):
    """One row per registered worker instance."""

    __tablename__ = "workers"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_workers_ws_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[WorkerStatus] = mapped_column(
        SAEnum(WorkerStatus, name="workers_worker_status_enum"),
        nullable=False,
        default=WorkerStatus.IDLE,
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class WorkerInstallTokenRow(WorkersBase):
    """One-shot install token issued to bootstrap a new worker.

    The token itself is never stored — only ``token_hash`` (HMAC) is
    persisted so leakage of the row doesn't leak the token.
    """

    __tablename__ = "worker_install_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_worker_install_tokens_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class AuditRelayStateRow(WorkersBase):
    """Singleton per workspace — high-water mark for audit relay."""

    __tablename__ = "audit_relay_state"

    workspace_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    last_relayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)


__all__ = [
    "AuditRelayStateRow",
    "WorkerInstallTokenRow",
    "WorkerRow",
    "WorkerStatus",
    "WorkersBase",
]
