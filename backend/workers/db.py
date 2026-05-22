"""Workers persistence schema — workers + install_tokens + audit_relay_state
+ settle_drains.

Workflow §12.5 #8 (Bundle G — Workers). Tracks worker registration,
install-token issuance, the audit-relay cursor, and the settle-drain
high-water marks that keep the BSage write subscriber idempotent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

WorkersBase = Base


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
        SAEnum(
            WorkerStatus,
            name="workers_worker_status_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
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


class SettleDrainRow(WorkersBase):
    """One row per ``settle`` activity already absorbed into BSage.

    The :class:`~backend.workers.settle_worker.SettleWorker` (the §4
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
    "AuditRelayStateRow",
    "SettleDrainRow",
    "WorkerInstallTokenRow",
    "WorkerRow",
    "WorkerStatus",
    "WorkersBase",
]
