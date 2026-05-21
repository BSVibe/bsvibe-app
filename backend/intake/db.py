"""Intake persistence schema — trigger_events + requests.

Workflow §12.5 #8 (Bundle G — Intake / Triggers). Per-workspace
scoping is enforced via ``workspace_id NOT NULL`` on every row plus a
composite unique on ``(workspace_id, source, idempotency_key)`` so
duplicate triggers fail at the DB layer regardless of intake surface.

We redeclare ``RequestStatus`` here as a local :class:`StrEnum` (rather
than importing ``backend.execution._domain.RequestStatus``) to keep
SQLAlchemy enum naming stable across module boundaries — see the
Phase 1 note in :mod:`backend.knowledge.canonicalization.db`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class IntakeBase(DeclarativeBase):
    """Declarative base for intake-domain tables."""


class TriggerKind(StrEnum):
    """Mirrors :data:`backend.intake.schema.TriggerKindLiteral`."""

    WEBHOOK = "webhook"
    SCHEDULE = "schedule"
    DIRECT = "direct"
    DECISION_RESOLUTION = "decision_resolution"


class RequestStatus(StrEnum):
    """Mirror of :class:`backend.execution._domain.RequestStatus`.

    Redeclared locally so SQLAlchemy owns a named Postgres ENUM scoped
    to the intake schema, decoupled from the execution module's enum
    lifecycle. Keep value strings in sync.
    """

    OPEN = "open"
    RUNNING = "running"
    NEEDS_DECISION = "needs_decision"
    REVIEW_READY = "review_ready"
    SHIPPED = "shipped"
    ABANDONED = "abandoned"


class TriggerEventRow(IntakeBase):
    """Raw inbound trigger envelope row — Workflow §3.1 persistence."""

    __tablename__ = "trigger_events"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "source",
            "idempotency_key",
            name="uq_trigger_events_ws_src_key",
        ),
        Index("ix_trigger_events_ws_received", "workspace_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger_kind: Mapped[TriggerKind] = mapped_column(
        SAEnum(
            TriggerKind,
            name="intake_trigger_kind_enum",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class RequestRow(IntakeBase):
    """``Request`` — the workflow state machine's unit of work.

    One TriggerEvent typically begets one Request; the Request is the
    durable handle the orchestrator (Bundle G — Orchestrator) drives
    through the 3+ε state machine (Workflow §1).
    """

    __tablename__ = "requests"
    __table_args__ = (
        Index("ix_requests_ws_status", "workspace_id", "status"),
        Index("ix_requests_trigger_event", "trigger_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    trigger_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trigger_events.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[RequestStatus] = mapped_column(
        SAEnum(
            RequestStatus,
            name="intake_request_status_enum",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=RequestStatus.OPEN,
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(),
        onupdate=lambda: datetime.now(),
    )


__all__ = [
    "IntakeBase",
    "RequestRow",
    "RequestStatus",
    "TriggerEventRow",
    "TriggerKind",
]
